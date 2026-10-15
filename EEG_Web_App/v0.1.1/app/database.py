from pathlib import Path

from sqlalchemy import create_engine, Column, Integer, String, Boolean, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

# Data lives next to the app package (v0.x/data), independent of the cwd.
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "app.db"

DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Subject(Base):
    __tablename__ = "subjects"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    age = Column(Integer, nullable=True)
    gender = Column(String, nullable=True)
    notes = Column(String, nullable=True)

    trials = relationship("Trial", back_populates="subject")


class Trial(Base):
    """One recording attempt.

    recording -- True once data has been recorded for this trial
                 (whether the recorder is *currently* running is
                 tracked in memory by LiveRecorderManager.is_recording)
    saved     -- True once the trial has been exported to XDF
    """
    __tablename__ = "trials"

    id = Column(Integer, primary_key=True, index=True)
    trial_number = Column(Integer, nullable=False, default=1)
    patient_id = Column(Integer, ForeignKey("subjects.id"), nullable=False)
    hardware_prop_name = Column(String, nullable=False, default="name")
    hardware_prop_value = Column(String, nullable=False, default="AURA")
    vr_prop_name = Column(String, nullable=False, default="name")
    vr_prop_value = Column(String, nullable=False, default="VR_Kitchen_Markers")
    recording = Column(Boolean, nullable=False, default=False)
    saved = Column(Boolean, nullable=False, default=False)
    file_name_raw = Column(String, nullable=True)
    file_name_all = Column(String, nullable=True)

    subject = relationship("Subject", back_populates="trials")


def init_db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if not db.query(Subject).filter(Subject.id == 1).first():
            db.add(Subject(id=1, name="Default Subject", age=25, gender="Other",
                           notes="Auto-initialized default subject"))
            db.commit()

        if not db.query(Trial).first():
            # Empty, unrecorded trial #1 ready to be used by the first recording.
            db.add(Trial(id=1, trial_number=1, patient_id=1,
                         hardware_prop_name="name", hardware_prop_value="AURA",
                         vr_prop_name="name", vr_prop_value="VR_Kitchen_Markers",
                         recording=False, saved=False))
            db.commit()
    finally:
        db.close()
