from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class SubjectCreate(BaseModel):
    name: str = Field(..., min_length=1)
    age: Optional[int] = Field(None, ge=0, le=130)
    gender: Optional[str] = None
    notes: Optional[str] = None


class SubjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    age: Optional[int] = None
    gender: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[str] = None
    trial_count: int = 0
    last_trial_at: Optional[str] = None
    next_trial_number: int = 1


class FilterParams(BaseModel):
    notch_freq: float = 60.0
    locut: float = 0.5
    hicut: float = 40.0


class StreamConfig(BaseModel):
    hardware_prop_name: str = "name"
    hardware_prop_value: str = "AURA"
    vr_prop_name: str = "name"
    vr_prop_value: str = "VR_Vending_Machine_Markers"


class ConnectRequest(StreamConfig):
    filters: List[str] = []
    filter_params: FilterParams = FilterParams()


class FiltersRequest(BaseModel):
    filters: List[str] = []
    filter_params: FilterParams = FilterParams()


class RecordingStartRequest(BaseModel):
    patient_id: int
    trial_number: int = Field(..., ge=0)
    session_id: Optional[int] = None
    kind: Optional[str] = Field(None, pattern="^(task|baseline)$")
    duration_s: Optional[int] = Field(None, ge=0)     # stop automatically after this time


class ExportRequest(BaseModel):
    export_type: str = Field(..., pattern="^(raw|all)$")


class TrialNotesUpdate(BaseModel):
    notes: Optional[str] = None


class NoteRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=200)


class MontageRequest(BaseModel):
    labels: List[str]
    frontal: Optional[int] = None
    parietal: Optional[int] = None


class ExperimentIn(BaseModel):
    name: str = Field(..., min_length=1)
    description: Optional[str] = None
    n_trials: int = Field(3, ge=1, le=100)
    trial_duration_s: int = Field(0, ge=0, le=6 * 3600)
    baseline_s: int = Field(0, ge=0, le=3600)
    baseline_type: Optional[str] = "eyes_open"
    rest_s: int = Field(30, ge=0, le=3600)
    checklist: List[str] = []
    hardware_prop_name: str = "name"
    hardware_prop_value: str = "AURA"
    vr_prop_name: str = "name"
    vr_prop_value: str = "VR_Vending_Machine_Markers"
    filters: List[str] = ["notch", "bp"]
    filter_params: FilterParams = FilterParams()
    channels_used: int = Field(5, ge=1, le=64)
    channel_labels: List[str] = []
    target_participants: Optional[int] = Field(None, ge=1)
    instructions: Optional[str] = None


class SessionCreate(BaseModel):
    experiment_id: int
    patient_id: int
    operator: Optional[str] = None
    checklist_done: List[str] = []


class SessionUpdate(BaseModel):
    status: Optional[str] = Field(None, pattern="^(in_progress|completed|aborted)$")
    notes: Optional[str] = None
    checklist_done: Optional[List[str]] = None
