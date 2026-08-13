from datetime import datetime
from pydantic import BaseModel, ConfigDict

class ArtifactOut(BaseModel):
    id: str
    case_id: str
    analyzer_run_id: str | None = None
    evidence_id: str | None = None
    type: str
    filename: str
    content_type: str | None = None
    size_bytes: int
    sha256: str
    metadata_json: dict | None = None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)
