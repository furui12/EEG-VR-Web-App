from pydantic import BaseModel, Field
from typing import Optional, List

class SubjectCreate(BaseModel):
    name: str
    age: Optional[int] = None
    gender: Optional[str] = None
    notes: Optional[str] = None

class SubjectResponse(BaseModel):
    id: int
    name: str
    age: Optional[int] = None
    gender: Optional[str] = None
    notes: Optional[str] = None

    class Config:
        from_attributes = True

class TrialConfigUpdate(BaseModel):
    trial_number: int
    patient_id: int
    hardware_prop_name: str
    hardware_prop_value: str
    vr_prop_name: str
    vr_prop_value: str

class TrialResponse(BaseModel):
    id: int
    trial_number: int
    patient_id: int
    hardware_prop_name: str
    hardware_prop_value: str
    vr_prop_name: str
    vr_prop_value: str
    recording: bool
    saved: bool
    file_name_raw: Optional[str] = None
    file_name_all: Optional[str] = None

    class Config:
        from_attributes = True

class RecordingControlRequest(BaseModel):
    filters: List[str] = []

class ExportRequest(BaseModel):
    export_type: str = Field(..., pattern="^(raw|all)$")

class NewTrialApprovalRequest(BaseModel):
    approved: bool