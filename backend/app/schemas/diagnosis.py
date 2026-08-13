from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict, Field, field_validator
from app.contracts.enums import DiagnosisRunStatus, HypothesisState

class DiagnosisStartRequest(BaseModel):
    auto_execute_low_risk: bool = True

class DiagnosisRunOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id:str; case_id:str; job_id:str|None; status:DiagnosisRunStatus; cycle:int; reasoner_name:str; reasoner_version:str; workflow_version:str
    last_fingerprint:str|None=None; no_progress_count:int; summary_json:dict|None=None; decision_json:dict|None=None
    started_at:datetime|None=None; finished_at:datetime|None=None; created_at:datetime; updated_at:datetime

class HypothesisOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id:str; case_id:str; diagnosis_run_id:str|None; code:str; title:str; fault_domain:str; status:HypothesisState
    confidence:float; rationale:str|None=None; confirmable:bool; confirm_rule:str|None=None; created_at:datetime; updated_at:datetime
    @field_validator('confidence',mode='before')
    @classmethod
    def normalize_confidence(cls,v):
        x=float(v); return x/10000.0 if x>1 else x
    @field_validator('confirmable',mode='before')
    @classmethod
    def normalize_confirmable(cls,v): return bool(v)

class HypothesisRevisionOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id:str; hypothesis_id:str; diagnosis_run_id:str|None; revision_no:int; supersedes_revision_id:str|None
    title:str; fault_domain:str; status:HypothesisState; confidence:float; rationale:str|None=None; confirmable:bool; confirm_rule:str|None=None; created_at:datetime
    @field_validator('confidence',mode='before')
    @classmethod
    def normalize_confidence(cls,v):
        x=float(v); return x/10000.0 if x>1 else x
    @field_validator('confirmable',mode='before')
    @classmethod
    def normalize_confirmable(cls,v): return bool(v)

class HypothesisEvidenceOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id:str; hypothesis_id:str; ref_type:str; ref_id:str; evidence_level:str; direction:str; weight:int; rationale:str|None=None; details_json:dict|None=None; created_at:datetime

class CollectionPlanOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id:str; case_id:str; diagnosis_run_id:str; cycle:int; status:str; goal:str; actions_json:list; execution_job_ids:list|None=None; created_at:datetime; updated_at:datetime

class ConfirmHypothesisRequest(BaseModel):
    actor:str=Field(min_length=1,max_length=128)
    note:str|None=None
