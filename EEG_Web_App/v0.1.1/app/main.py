from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import DATA_DIR, SessionLocal, Subject, Trial, init_db
from app.models import (
    ExportRequest, NewTrialApprovalRequest, RecordingControlRequest,
    SubjectCreate, SubjectResponse, TrialConfigUpdate,
)
from app.signal_pipeline import StreamNotFoundError, manager

BASE_DIR = Path(__file__).resolve().parent


def _next_trial(prev, **overrides):
    """New, unrecorded trial that inherits the configuration of `prev`."""
    fields = dict(
        trial_number=prev.trial_number + 1,
        patient_id=prev.patient_id,
        hardware_prop_name=prev.hardware_prop_name,
        hardware_prop_value=prev.hardware_prop_value,
        vr_prop_name=prev.vr_prop_name,
        vr_prop_value=prev.vr_prop_value,
        recording=False,
        saved=False,
    )
    fields.update(overrides)
    return Trial(**fields)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    db = SessionLocal()
    try:
        last = db.query(Trial).order_by(Trial.id.desc()).first()
        if last and last.recording and last.saved:
            db.add(_next_trial(last))
            db.commit()
    finally:
        db.close()
    yield
    manager.stop_recording()


app = FastAPI(title="EEG VR Real-Time Pipeline", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _last_trial(db):
    return db.query(Trial).order_by(Trial.id.desc()).first()


@app.get("/", response_class=HTMLResponse)
def read_root(request: Request):
    return templates.TemplateResponse(request, "index.html")


# ------------------------------------------------------------------ subjects
@app.get("/api/subjects", response_model=list[SubjectResponse])
def get_subjects(db: Session = Depends(get_db)):
    return db.query(Subject).all()


@app.post("/api/subjects", response_model=SubjectResponse)
def create_subject(subj: SubjectCreate, db: Session = Depends(get_db)):
    if db.query(Subject).filter(Subject.name == subj.name).first():
        raise HTTPException(status_code=400, detail="Subject already exists.")
    new_subj = Subject(name=subj.name, age=subj.age, gender=subj.gender, notes=subj.notes)
    db.add(new_subj)
    db.commit()
    db.refresh(new_subj)
    return new_subj


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

    hw_names = uniq(["name", "source_id", "type"] + [t.hardware_prop_name for t in trials])
    vr_names = uniq(["name", "source_id", "type"] + [t.vr_prop_name for t in trials])
    hw_values = uniq(["AURA"] + [t.hardware_prop_value for t in trials])
    vr_values = uniq(["VR_Kitchen_Markers"] + [t.vr_prop_value for t in trials])
    for s in discovered:
        target = vr_values if s["type"].lower() in ("markers", "marker", "events") else hw_values
        for v in (s["name"], s["source_id"]):
            if v and v not in target:
                target.append(v)

    return {
        "discovered": discovered,
        "options": {
            "hardware_prop_names": hw_names,
            "hardware_prop_values": hw_values,
            "vr_prop_names": vr_names,
            "vr_prop_values": vr_values,
        },
    }


# -------------------------------------------------------------------- trials
@app.get("/api/trials/current", response_model=dict)
def get_current_trial(db: Session = Depends(get_db)):
    last = _last_trial(db)
    if not last:
        last = Trial(trial_number=1, patient_id=1, hardware_prop_name="name",
                     hardware_prop_value="AURA", vr_prop_name="name",
                     vr_prop_value="VR_Kitchen_Markers", recording=False, saved=False)
        db.add(last)
        db.commit()
        db.refresh(last)

    subject = db.query(Subject).filter(Subject.id == last.patient_id).first()
    return {
        "trial": {
            "id": last.id,
            "trial_number": last.trial_number,
            "patient_id": last.patient_id,
            "patient_name": subject.name if subject else "Unknown",
            "hardware_prop_name": last.hardware_prop_name,
            "hardware_prop_value": last.hardware_prop_value,
            "vr_prop_name": last.vr_prop_name,
            "vr_prop_value": last.vr_prop_value,
            "recording": last.recording,
            "saved": last.saved,
            "file_name_raw": last.file_name_raw,
            "file_name_all": last.file_name_all,
        },
        "is_recording": manager.is_recording,
        "has_data": manager.has_data(last.id),
    }


@app.post("/api/trials/config")
def save_apply_config(config: TrialConfigUpdate, db: Session = Depends(get_db)):
    if manager.is_recording:
        raise HTTPException(status_code=400, detail="Stop the recording before changing the configuration.")
    if not db.query(Subject).filter(Subject.id == config.patient_id).first():
        raise HTTPException(status_code=400, detail="Unknown subject.")

    values = config.model_dump()
    last = _last_trial(db)
    if last is None or last.saved:
        # Never overwrite the metadata of a trial that was already exported.
        last = Trial(**values, recording=False, saved=False)
        db.add(last)
    else:
        for key, value in values.items():
            setattr(last, key, value)
    db.commit()
    db.refresh(last)
    return {"status": "success", "trial_id": last.id}


@app.post("/api/trials/new-approval")
def approve_new_trial(req: NewTrialApprovalRequest, db: Session = Depends(get_db)):
    if not req.approved:
        return {"status": "cancelled"}
    last = _last_trial(db)
    if last:
        db.add(_next_trial(last))
        db.commit()
    return {"status": "approved"}


# ----------------------------------------------------------------- recording
@app.post("/api/recording/start")
def start_recording(req: RecordingControlRequest, db: Session = Depends(get_db)):
    if manager.is_recording:
        raise HTTPException(status_code=400, detail="A recording is already running.")

    last = _last_trial(db)
    if last and last.recording and not last.saved:
        return JSONResponse(status_code=400, content={
            "error": "unsaved_recording",
            "message": "The previous recording was not saved. Do you want to initiate a new "
                       "recording and abandon saving the previous data?",
        })

    trial = _next_trial(last) if (last and last.saved) else last
    if trial is None:
        raise HTTPException(status_code=400, detail="No trial configured.")
    db.add(trial)
    db.flush()  # assigns trial.id without committing

    lr_file = str(DATA_DIR / f"trial_{trial.trial_number}_subject_{trial.patient_id}_labrecorder.xdf")
    try:
        info = manager.start_recording(
            trial.id, trial.hardware_prop_name, trial.hardware_prop_value,
            trial.vr_prop_name, trial.vr_prop_value, req.filters, labrecorder_file=lr_file)
    except StreamNotFoundError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Could not start recording: {exc}")

    trial.recording = True
    trial.saved = False
    db.commit()
    return {"status": "started", "trial_id": trial.id, **info}


@app.post("/api/recording/stop")
def stop_recording():
    # Trial.recording stays True: it means "this trial has recorded data".
    return {"status": "stopped", **manager.stop_recording()}


@app.get("/api/data/live")
def get_live_data(since: int = 0):
    return manager.get_live(since)


# -------------------------------------------------------------------- export
@app.post("/api/export")
def export_data(req: ExportRequest, db: Session = Depends(get_db)):
    last = _last_trial(db)
    if manager.is_recording:
        raise HTTPException(status_code=400, detail="Cannot export while recording.")
    if not last or not last.recording:
        raise HTTPException(status_code=400, detail="The current trial has no recorded data.")

    filename = f"trial_{last.trial_number}_subject_{last.patient_id}_{req.export_type}.xdf"
    full_path = DATA_DIR / filename
    try:
        summary = manager.export_xdf(last.id, req.export_type, str(full_path))
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if req.export_type == "raw":
        last.file_name_raw = filename
    else:
        last.file_name_all = filename
    last.saved = True
    db.commit()
    return {"status": "success", "filename": filename, "path": str(full_path), **summary}
