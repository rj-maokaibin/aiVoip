from typing import Any
from pydantic import BaseModel


class FeishuCardPreviewOut(BaseModel):
    case_id: str
    delivery_mode: str = "MOCK"
    card: dict[str, Any]


class FeishuSyncRequest(BaseModel):
    receive_id: str | None = None
    receive_id_type: str | None = None


class FeishuSyncOut(BaseModel):
    case_id: str
    message_id: str
    receive_id: str
    receive_id_type: str
    status: str
    card_version: int


class FeishuCallbackOut(BaseModel):
    code: int = 0
    msg: str = "ok"
    toast: dict[str, Any] | None = None
