from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

class RuleUpsertRequest(BaseModel):
    rule: dict[str,Any]
    actor: str=Field(min_length=1,max_length=128)
    change_note: str|None=None
    activate: bool=False

class RuleActivateRequest(BaseModel):
    actor: str=Field(min_length=1,max_length=128)

class RuleReplayRequest(BaseModel):
    case_id: str
    actor: str=Field(min_length=1,max_length=128)

class RuleVersionOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id:str; rule_definition_id:str; version:str; checksum:str; status:str; content_json:dict
    created_by:str|None=None; approved_by:str|None=None; change_note:str|None=None; created_at:datetime; approved_at:datetime|None=None
