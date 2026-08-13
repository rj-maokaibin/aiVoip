from datetime import datetime
from pydantic import BaseModel, ConfigDict
from app.contracts.enums import DependencyPolicy, JobStatus


class JobOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id:str; case_id:str; type:str; status:JobStatus; profile_id:str|None; error_code:str|None; error_message:str|None; created_at:datetime; started_at:datetime|None; finished_at:datetime|None


class JobDependencyCreate(BaseModel):
    depends_on_job_id: str
    policy: DependencyPolicy = DependencyPolicy.WAIT_ALL_SUCCESS


class JobDependencyOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id: str
    job_id: str
    depends_on_job_id: str
    policy: DependencyPolicy
    created_at: datetime
