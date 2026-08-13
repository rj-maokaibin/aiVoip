from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

class KnowledgeCreateRequest(BaseModel):
    type:str=Field(min_length=1,max_length=64)
    title:str=Field(min_length=1,max_length=512)
    summary:str=Field(min_length=1,max_length=12000)
    content_json:dict[str,Any]|None=None
    tags:list[str]=[]
    source_ref:str|None=None
    verified:bool=False
    actor:str=Field(min_length=1,max_length=128)

class KnowledgeOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id:str; type:str; title:str; summary:str; content_json:dict|None=None; tags_json:list|None=None; source_ref:str|None=None
    status:str; verified:bool; verified_by:str|None=None; verified_at:datetime|None=None; created_by:str|None=None; created_at:datetime; updated_at:datetime

class KnowledgeVerifyRequest(BaseModel):
    actor:str=Field(min_length=1,max_length=128)
