from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field

class ReportGenerateRequest(BaseModel):
    actor:str=Field(min_length=1,max_length=128)

class DiagnosisReportOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id:str; case_id:str; diagnosis_run_id:str|None=None; version:str; status:str; html_object_key:str; json_object_key:str; snapshot_json:dict|None=None; created_by:str|None=None; created_at:datetime
