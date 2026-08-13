from datetime import datetime
from pydantic import BaseModel, ConfigDict

from app.contracts.enums import EvidenceCompleteness, EvidenceKind, EvidenceLevel, EvidenceScope


class EvidenceOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id:str; case_id:str; device_id:str|None; job_id:str|None; action_run_id:str|None
    type:str; source:str; kind:EvidenceKind; source_scope:EvidenceScope; level:EvidenceLevel; completeness:EvidenceCompleteness
    filename:str; size_bytes:int; sha256:str; content_type:str|None
    captured_at:datetime|None=None; time_range_start:datetime|None=None; time_range_end:datetime|None=None
    producer_type:str|None=None; producer_id:str|None=None; producer_version:str|None=None
    session_id:str|None=None; attempt_id:str|None=None; call_id:str|None=None
    metadata_json:dict|None; created_at:datetime
