from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import READ_ROLES, get_db, require_roles
from app.db.models import Evidence
from app.integrations.storage import ObjectStorage
from app.services.evidence_retention import ensure_retention_state

router=APIRouter(prefix='/evidences', tags=['evidences'])


@router.get('/{evidence_id}/download')
def download(evidence_id:str, db:Session=Depends(get_db), _identity=Depends(require_roles(*READ_ROLES))):
    row=db.get(Evidence, evidence_id)
    if not row:
        raise HTTPException(404,'EVIDENCE_NOT_FOUND')
    retention=ensure_retention_state(db,row)
    metadata=row.metadata_json or {}
    if str(retention.status or '').upper()=='EXPIRED' or metadata.get('payload_available') is False:
        db.commit()
        raise HTTPException(410,'EVIDENCE_PAYLOAD_EXPIRED')
    db.commit()
    return {'url':ObjectStorage().presigned_get(row.object_key)}
