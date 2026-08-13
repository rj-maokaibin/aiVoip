from datetime import datetime
from pydantic import BaseModel, ConfigDict

from app.contracts.enums import EvidenceScope, RunStatus


class AnalyzerRunOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id:str
    case_id:str
    job_id:str|None
    analyzer_name:str
    analyzer_version:str
    config_version:str|None
    config_checksum:str|None=None
    config_snapshot:dict|None=None
    scope:EvidenceScope
    status:RunStatus
    input_evidence_ids:list
    output_evidence_ids:list|None=None
    summary_json:dict|None
    error_code:str|None
    error_message:str|None
    started_at:datetime|None
    finished_at:datetime|None
    created_at:datetime
