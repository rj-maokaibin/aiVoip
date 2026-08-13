from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import READ_ROLES, get_db, require_roles
from app.db.models import AuditLog
from app.schemas.audit import AuditLogOut
from app.schemas.common import CursorPage
from app.core.pagination import paginate_created

router = APIRouter(tags=['audit'])


@router.get('/cases/{case_id}/audit', response_model=CursorPage[AuditLogOut])
def case_audit(case_id: str, limit:int=Query(default=100,ge=1,le=200), cursor:str|None=Query(default=None), db: Session = Depends(get_db), _identity=Depends(require_roles(*READ_ROLES))):
    items,next_cursor,has_more=paginate_created(db,AuditLog,where=(AuditLog.case_id==case_id,),limit=limit,cursor=cursor,descending=False)
    return CursorPage[AuditLogOut](items=[AuditLogOut.model_validate(x) for x in items],next_cursor=next_cursor,has_more=has_more)
