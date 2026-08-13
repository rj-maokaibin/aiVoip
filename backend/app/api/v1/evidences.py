from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import READ_ROLES, get_db, require_roles
from app.db.models import Evidence
from app.integrations.storage import ObjectStorage

router=APIRouter(prefix='/evidences', tags=['evidences'])


@router.get('/{evidence_id}/download')
def download(evidence_id:str, db:Session=Depends(get_db), _identity=Depends(require_roles(*READ_ROLES))):
    row=db.get(Evidence, evidence_id)
    if not row: raise HTTPException(404,'EVIDENCE_NOT_FOUND')
    return {'url':ObjectStorage().presigned_get(row.object_key)}
