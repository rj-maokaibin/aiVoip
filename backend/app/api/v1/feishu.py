from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_permissions
from app.contracts.enums import PermissionName
from app.core.errors import AppError
from app.integrations.feishu.cards import FeishuCaseCardBuilder
from app.schemas.feishu import FeishuCardPreviewOut, FeishuSyncRequest, FeishuSyncOut
from app.integrations.feishu.service import FeishuCaseCardService
from app.integrations.feishu.transport import FeishuTransportError

router = APIRouter(tags=["feishu"])

@router.get('/cases/{case_id}/feishu/card-preview', response_model=FeishuCardPreviewOut)
def preview_case_card(case_id: str, db: Session = Depends(get_db), _identity=Depends(require_permissions(PermissionName.CASE_READ))):
    try:
        built = FeishuCaseCardBuilder().build(db, case_id)
    except KeyError:
        raise AppError('CASE_NOT_FOUND')
    return FeishuCardPreviewOut(case_id=case_id, card=built.card)


@router.post('/cases/{case_id}/feishu/sync', response_model=FeishuSyncOut)
async def sync_case_card(case_id: str, req: FeishuSyncRequest, db: Session = Depends(get_db), _identity=Depends(require_permissions(PermissionName.CASE_WRITE))):
    try:
        row = await FeishuCaseCardService().sync_case_card(db, case_id=case_id, receive_id=req.receive_id, receive_id_type=req.receive_id_type)
        db.commit()
    except KeyError:
        raise AppError('CASE_NOT_FOUND')
    except (FeishuTransportError, ValueError) as exc:
        raise AppError('FEISHU_TRANSPORT_NOT_CONFIGURED', details={'reason': str(exc)}) from exc
    return FeishuSyncOut(case_id=case_id, message_id=row.message_id, receive_id=row.receive_id, receive_id_type=row.receive_id_type, status=row.status, card_version=row.card_version)
