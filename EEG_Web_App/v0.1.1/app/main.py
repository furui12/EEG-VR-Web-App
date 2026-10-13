from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
import os

from app.database import init_db, SessionLocal, Subject, Trial
from app.models import (
    SubjectCreate, SubjectResponse, TrialConfigUpdate, TrialResponse,
    RecordingControlRequest, ExportRequest, NewTrialApprovalRequest
)
from app.signal_pipeline import manager

app = FastAPI(title="EEG VR Real-Time Pipeline")

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

@app.on_event("startup")
def startup_event():
    init_db()
    db = SessionLocal()
    try:
        last_trial = db.query(Trial).order_by(Trial.id.desc()).first()
        if last_trial and last_trial.recording and last_trial.saved:
            new_trial = Trial(
                trial_number=last_trial.trial_number + 1,
                patient_id=last_trial.patient_id,
                hardware_prop_name=last_trial.hardware_prop_name,
                hardware_prop_value=last_trial.hardware_prop_value,
                vr_prop_name=last_trial.vr_prop_name,
                vr_prop_value=last_trial.vr_prop_value,
                recording=False,
                saved=False
            )
            db.add(new_trial)
            db.commit()
    finally:
        db.close()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.get("/", response_class=HTMLResponse)
def read_root(request: Request):
    # Updated to pass 'request' as the first argument for modern Starlette/FastAPI versions
    return templates.TemplateResponse(request, "index.html")

@app.get("/api/subjects", response_model=list[SubjectResponse])
def get_subjects(db: Session = Depends(get_db)):
    return db.query(Subject).all()

@app.post("/api/subjects", response_model=SubjectResponse)
def create_subject(subj: SubjectCreate, db: Session = Depends(get_db)):
    existing = db.query(Subject).filter(Subject.name == subj.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Subject already exists.")
    new_subj = Subject(name=subj.name, age=subj.age, gender=subj.gender, notes=subj.notes)
    db.add(new_subj)
    db.commit()
    db.refresh(new_subj)
    return new_subj

@app.get("/api/streams")
def get_lsl_streams(db: Session = Depends(get_db)):
    discovered = manager.discover_streams()
    trials = db.query(Trial).all()
    hw_names = list(set([t.hardware_prop_name for t in trials]))
    hw_values = list(set([t.hardware_prop_value for t in trials]))
    vr_names = list(set([t.vr_prop_name for t in trials]))
    vr_values = list(set([t.vr_prop_value for t in trials]))

    for s in discovered:
        if s["name"] not in hw_values:
            hw_values.append(s["name"])
        if s["source_id"] not in hw_values:
            hw_values.append(s["source_id"])

    return {
        "discovered": discovered,
        "options": {
            "hardware_prop_names": hw_names or ["name", "source_id"],
            "hardware_prop_values": hw_values or ["AURA"],
            "vr_prop_names": vr_names or ["name", "source_id"],
            "vr_prop_values": vr_values or ["VR_Kitchen_Markers"]
        }
    }

@app.get("/api/trials/current", response_model=dict)
def get_current_trial(db: Session = Depends(get_db)):
    last_trial = db.query(Trial).order_by(Trial.id.desc()).first()
    if not last_trial:
        last_trial = Trial(
            trial_number=1,
            patient_id=1,
            hardware_prop_name="name",
            hardware_prop_value="AURA",
            vr_prop_name="name",
            vr_prop_value="VR_Kitchen_Markers",
            recording=False,
            saved=True
        )
        db.add(last_trial)
        db.commit()
        db.refresh(last_trial)
    
    subject = db.query(Subject).filter(Subject.id == last_trial.patient_id).first()
    
    return {
        "trial": {
            "id": last_trial.id,
            "trial_number": last_trial.trial_number,
            "patient_id": last_trial.patient_id,
            "patient_name": subject.name if subject else "Unknown",
            "hardware_prop_name": last_trial.hardware_prop_name,
            "hardware_prop_value": last_trial.hardware_prop_value,
            "vr_prop_name": last_trial.vr_prop_name,
            "vr_prop_value": last_trial.vr_prop_value,
            "recording": last_trial.recording,
            "saved": last_trial.saved,
            "file_name_raw": last_trial.file_name_raw,
            "file_name_all": last_trial.file_name_all
        },
        "is_recording": manager.is_recording
    }

@app.post("/api/trials/config")
def save_apply_config(config: TrialConfigUpdate, db: Session = Depends(get_db)):
    last_trial = db.query(Trial).order_by(Trial.id.desc()).first()
    if last_trial:
        last_trial.trial_number = config.trial_number
        last_trial.patient_id = config.patient_id
        last_trial.hardware_prop_name = config.hardware_prop_name
        last_trial.hardware_prop_value = config.hardware_prop_value
        last_trial.vr_prop_name = config.vr_prop_name
        last_trial.vr_prop_value = config.vr_prop_value
        db.commit()
        db.refresh(last_trial)
    return {"status": "success", "trial_id": last_trial.id if last_trial else None}

@app.post("/api/recording/start")
def start_recording(req: RecordingControlRequest, db: Session = Depends(get_db)):
    last_trial = db.query(Trial).order_by(Trial.id.desc()).first()
    
    if last_trial and last_trial.recording and not last_trial.saved:
        return JSONResponse(status_code=400, content={
            "error": "unsaved_recording",
            "message": "The previous recording was not saved. Do you want to initiate a new recording and abandon saving the previous data?"
        })

    if last_trial and last_trial.saved:
        new_trial = Trial(
            trial_number=last_trial.trial_number + 1,
            patient_id=last_trial.patient_id,
            hardware_prop_name=last_trial.hardware_prop_name,
            hardware_prop_value=last_trial.hardware_prop_value,
            vr_prop_name=last_trial.vr_prop_name,
            vr_prop_value=last_trial.vr_prop_value,
            recording=True,
            saved=False
        )
        db.add(new_trial)
        db.commit()
        last_trial = new_trial
    elif last_trial:
        last_trial.recording = True
        last_trial.saved = False
        db.commit()

    manager.start_recording(last_trial.hardware_prop_name, last_trial.hardware_prop_value, req.filters)
    return {"status": "started", "trial_id": last_trial.id if last_trial else 1}

@app.post("/api/trials/new-approval")
def approve_new_trial(req: NewTrialApprovalRequest, db: Session = Depends(get_db)):
    last_trial = db.query(Trial).order_by(Trial.id.desc()).first()
    if not req.approved:
        return {"status": "cancelled"}
    
    if last_trial:
        new_trial = Trial(
            trial_number=last_trial.trial_number + 1,
            patient_id=last_trial.patient_id,
            hardware_prop_name=last_trial.hardware_prop_name,
            hardware_prop_value=last_trial.hardware_prop_value,
            vr_prop_name=last_trial.vr_prop_name,
            vr_prop_value=last_trial.vr_prop_value,
            recording=False,
            saved=False
        )
        db.add(new_trial)
        db.commit()
    return {"status": "approved"}

@app.post("/api/recording/stop")
def stop_recording(db: Session = Depends(get_db)):
    manager.stop_recording()
    last_trial = db.query(Trial).order_by(Trial.id.desc()).first()
    if last_trial:
        last_trial.recording = False
        db.commit()
    return {"status": "stopped"}

@app.get("/api/data/live")
def get_live_data():
    return {
        "chunk": manager.latest_chunk,
        "timestamps": manager.latest_timestamps,
        "is_recording": manager.is_recording
    }

@app.post("/api/export")
def export_data(req: ExportRequest, db: Session = Depends(get_db)):
    last_trial = db.query(Trial).order_by(Trial.id.desc()).first()
    if not last_trial or last_trial.recording:
        raise HTTPException(status_code=400, detail="Cannot export while recording or without an active trial.")

    filename = f"trial_{last_trial.trial_number}_subject_{last_trial.patient_id}_{req.export_type}.xdf"
    full_path = manager.export_xdf(
        filename,
        req.export_type,
        last_trial.hardware_prop_name,
        last_trial.hardware_prop_value,
        last_trial.vr_prop_name,
        last_trial.vr_prop_value
    )

    if req.export_type == "raw":
        last_trial.file_name_raw = filename
    else:
        last_trial.file_name_all = filename

    last_trial.saved = True
    db.commit()

    return {"status": "success", "filename": filename, "path": full_path}