from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict

from app.contracts.enums import CaseStatus


class CaseCreate(BaseModel):
    summary: str = Field(min_length=2)
    ip: str
    ssh_port: int = Field(default=22, ge=1, le=65535)
    sn: str = Field(min_length=1)
    created_by: str | None = None


class DeviceOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id: str; ip: str; ssh_port: int; sn: str; username: str; platform_id: str|None=None; device_info: dict|None=None


class CaseOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id: str; case_no: str; summary: str; status: CaseStatus; created_by: str|None; created_at: datetime; updated_at: datetime
    devices: list[DeviceOut]=[]


class CollectRequest(BaseModel):
    profile_id: str = 'voip_basic'
