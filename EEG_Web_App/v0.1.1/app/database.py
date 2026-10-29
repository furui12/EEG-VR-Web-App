from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Boolean, Column, Float, ForeignKey, Integer, String, create_engine, inspect, text,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

# Data lives next to the app package (v0.x/data), independent of the cwd.
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "app.db"

DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


class Subject(Base):
    __tablename__ = "subjects"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    age = Column(Integer, nullable=True)
    gender = Column(String, nullable=True)
    notes = Column(String, nullable=True)
    created_at = Column(String, nullable=True, default=now_iso)

    trials = relationship("Trial", back_populates="subject")


class Trial(Base):
    """One recording.

    A row is created when a recording starts.
    recording -- True once data has been recorded for this trial
    saved     -- True once the trial has been written to an XDF file
    """
    __tablename__ = "trials"

    id = Column(Integer, primary_key=True, index=True)
    trial_number = Column(Integer, nullable=False, default=1)
    patient_id = Column(Integer, ForeignKey("subjects.id"), nullable=False)
    hardware_prop_name = Column(String, nullable=False, default="name")
    hardware_prop_value = Column(String, nullable=False, default="AURA")
    vr_prop_name = Column(String, nullable=False, default="name")
    vr_prop_value = Column(String, nullable=False, default="VR_Vending_Machine_Markers")
    recording = Column(Boolean, nullable=False, default=False)
    saved = Column(Boolean, nullable=False, default=False)
    file_name_raw = Column(String, nullable=True)
    file_name_all = Column(String, nullable=True)
    started_at = Column(String, nullable=True)
    duration_s = Column(Float, nullable=True)
    n_samples = Column(Integer, nullable=True)
    n_markers = Column(Integer, nullable=True)
    n_notes = Column(Integer, nullable=True)
    filters = Column(String, nullable=True)
    notes = Column(String, nullable=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=True)
    kind = Column(String, nullable=True)          # task | baseline (operator sessions)
    stats = Column(String, nullable=True)         # JSON: signal quality and metrics summary

    subject = relationship("Subject", back_populates="trials")


class Experiment(Base):
    """A protocol defined once and then run by the operator: baseline, trials, rests,
    checklist, streams and pre-processing (applied automatically)."""
    __tablename__ = "experiments"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    n_trials = Column(Integer, nullable=False, default=3)
    trial_duration_s = Column(Integer, nullable=False, default=0)    # 0 = stop manually
    baseline_s = Column(Integer, nullable=False, default=0)          # 0 = no baseline
    baseline_type = Column(String, nullable=True, default="eyes_open")
    rest_s = Column(Integer, nullable=False, default=30)
    checklist = Column(String, nullable=True)                        # JSON list of strings
    hardware_prop_name = Column(String, nullable=False, default="name")
    hardware_prop_value = Column(String, nullable=False, default="AURA")
    vr_prop_name = Column(String, nullable=False, default="name")
    vr_prop_value = Column(String, nullable=False, default="VR_Vending_Machine_Markers")
    filters = Column(String, nullable=True)                          # JSON list
    filter_params = Column(String, nullable=True)                    # JSON dict
    channels_used = Column(Integer, nullable=True, default=5)        # first N channels of the EEG stream
    channel_labels = Column(String, nullable=True)                   # JSON list: names of those channels
    instructions = Column(String, nullable=True)                     # read to the participant before each trial
    target_participants = Column(Integer, nullable=True)
    archived = Column(Boolean, nullable=False, default=False)
    created_at = Column(String, nullable=True, default=now_iso)


class ExperimentSession(Base):
    """One participant going through one experiment (baseline + trials)."""
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True, index=True)
    experiment_id = Column(Integer, ForeignKey("experiments.id"), nullable=False)
    patient_id = Column(Integer, ForeignKey("subjects.id"), nullable=False)
    operator = Column(String, nullable=True)
    status = Column(String, nullable=False, default="in_progress")  # in_progress | completed | aborted
    started_at = Column(String, nullable=True, default=now_iso)
    ended_at = Column(String, nullable=True)
    checklist_done = Column(String, nullable=True)                   # JSON list
    notes = Column(String, nullable=True)


def _add_missing_columns():
    """Tiny migration: add columns introduced after the first release to existing DBs."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in existing:
                    col_type = col.type.compile(dialect=engine.dialect)
                    conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {col_type}'))


DEFAULT_CHECKLIST = [
    "Informed consent signed",
    "EEG headset placed and electrodes wetted / gelled",
    "Participant briefed and comfortable",
    "VR headset adjusted and the VR game open",
]

# The platform analyses the first 5 channels of the EEG stream: frontal electrodes.
DEFAULT_FRONTAL_LABELS = ["Fp1", "Fp2", "F3", "Fz", "F4"]

DEFAULT_EXPERIMENTS = [
    dict(name="VR Vending Machine",
         description="Resting baseline, then the vending-machine task in VR.",
         n_trials=3, trial_duration_s=300, baseline_s=60, baseline_type="eyes_open", rest_s=30,
         vr_prop_value="VR_Vending_Machine_Markers",
         instructions="Use the vending machine to get the items shown on the screen. Move calmly and tell me if the headset bothers you."),
    dict(name="VR Relaxation",
         description="Eyes-closed baseline, then guided relaxation scenes in VR.",
         n_trials=2, trial_duration_s=300, baseline_s=120, baseline_type="eyes_closed", rest_s=60,
         vr_prop_value="VR_Relaxation_Markers",
         instructions="Look around the scene, breathe slowly and relax. Try to keep your head still."),
]


def init_db():
    import json
    Base.metadata.create_all(bind=engine)
    _add_missing_columns()
    db = SessionLocal()
    try:
        # the old example protocol is retired when it was never used
        for old in db.query(Experiment).filter(Experiment.name == "VR Kitchen – cognitive load").all():
            if db.query(ExperimentSession).filter(ExperimentSession.experiment_id == old.id).count() == 0:
                old.archived = True
        for spec in DEFAULT_EXPERIMENTS:
            found = db.query(Experiment).filter(Experiment.name == spec["name"]).first()
            if found:
                if not json.loads(found.channel_labels or "[]"):     # created by an older version
                    found.channels_used = found.channels_used or 5
                    found.channel_labels = json.dumps(DEFAULT_FRONTAL_LABELS[: found.channels_used])
                continue
            db.add(Experiment(
                **spec, checklist=json.dumps(DEFAULT_CHECKLIST), channels_used=5,
                channel_labels=json.dumps(DEFAULT_FRONTAL_LABELS),
                filters=json.dumps(["notch", "bp"]),
                filter_params=json.dumps({"notch_freq": 60, "locut": 0.5, "hicut": 40}),
            ))
        db.commit()
    finally:
        db.close()
