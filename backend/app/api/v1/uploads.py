import hashlib
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.api.deps import ENGINEER_ROLES, get_db, require_roles
from app.contracts.enums import EvidenceCompleteness, EvidenceKind, EvidenceLevel, EvidenceScope
from app.core.ids import new_id
from app.db.models import Case
from app.integrations.storage import ObjectStorage
from app.schemas.evidence import EvidenceOut
from app.services.evidence import create_evidence
from app.services.audit import audit
from app.services.idempotency import begin_idempotent, complete_idempotent

router=APIRouter(tags=['evidences'])


def infer_type(filename:str) -> str:
    lower=filename.lower()
    if lower.endswith('.pcapng'): return 'PCAPNG'
    if lower.endswith('.pcap'): return 'PCAP'
    return 'USER_UPLOAD'


@router.post('/cases/{case_id}/evidences/upload', response_model=EvidenceOut, status_code=201)
async def upload_evidence(
    case_id:str,
    file:UploadFile=File(...),
    evidence_type:str|None=Form(None),
    idempotency_key:str|None=Header(default=None,alias='Idempotency-Key'),
    db:Session=Depends(get_db),
    identity=Depends(require_roles(*ENGINEER_ROLES)),
):
    case=db.get(Case, case_id)
    if not case: raise HTTPException(404,'CASE_NOT_FOUND')
    filename=Path(file.filename or 'upload.bin').name
    evidence_id=new_id(); sha=hashlib.sha256(); size=0
    with tempfile.NamedTemporaryFile(prefix='voip-upload-', delete=False) as tmp:
        tmp_path=Path(tmp.name)
        while chunk:=await file.read(1024*1024):
            sha.update(chunk); size+=len(chunk); tmp.write(chunk)
    digest=sha.hexdigest()
    handle=begin_idempotent(
        db,
        scope=f'POST:/api/v1/cases/{case_id}/evidences/upload',
        key=idempotency_key,
        payload={'case_id':case_id,'filename':filename,'evidence_type':evidence_type or infer_type(filename),'size_bytes':size,'sha256':digest,'content_type':file.content_type},
    )
    if handle.replay is not None:
        tmp_path.unlink(missing_ok=True)
        return handle.replay
    object_key=f'cases/{case_id}/evidence/{evidence_id}/{filename}'
    try:
        ObjectStorage().put_file(object_key,tmp_path,file.content_type or 'application/octet-stream')
    finally:
        tmp_path.unlink(missing_ok=True)
    now=datetime.now(timezone.utc)
    row=create_evidence(
        db,evidence_id=evidence_id,case_id=case_id,evidence_type=evidence_type or infer_type(filename),source='USER_UPLOAD',
        filename=filename,object_key=object_key,size_bytes=size,sha256=digest,content_type=file.content_type,
        kind=EvidenceKind.RAW,scope=EvidenceScope.CASE,level=EvidenceLevel.L1,completeness=EvidenceCompleteness.COMPLETE,
        captured_at=now,producer_type='USER',producer_id=identity.actor_id,producer_version='upload-v1',metadata={},actor=identity.actor_id,
    )
    audit(db,case_id=case_id,actor=identity.actor_id,event_type='EVIDENCE_UPLOADED',target_type='evidence',target_id=row.id,
          detail={'filename':filename,'size_bytes':size,'type':row.type,'kind':row.kind,'completeness':row.completeness})
    response=EvidenceOut.model_validate(row).model_dump(mode='json')
    complete_idempotent(db,handle,response=response,status_code=201,resource_type='evidence',resource_id=row.id)
    db.commit(); db.refresh(row)
    from app.workers.diagnosis_tasks import notify_case_changed
    notify_case_changed(case_id)
    return row
