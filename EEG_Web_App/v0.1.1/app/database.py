import os
from sqlalchemy import create_engine, Column, Integer, String, Boolean, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

DB_DIR = "data"
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "app.db")

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
        default_subj = db.query(Subject).filter(Subject.id == 1).first()
        if not default_subj:
            default_subj = Subject(id=1, name="Default Subject", age=25, gender="Other", notes="Auto-initialized default subject")
            db.add(default_subj)
            db.commit()

        default_trial = db.query(Trial).filter(Trial.id == 1).first()
        if not default_trial:
            default_trial = Trial(
                id=1,
                trial_number=1,
                patient_id=1,
                hardware_prop_name="name",
                hardware_prop_value="AURA",
                vr_prop_name="name",
                vr_prop_value="VR_Kitchen_Markers",
                recording=False,
                saved=True
            )
            db.add(default_trial)
            db.commit()
    finally:
        db.close()