from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import FeishuCaseBinding
from app.integrations.feishu.cards import FeishuCaseCardBuilder
from app.integrations.feishu.transport import FeishuLiveTransport


class FeishuCaseCardService:
    async def sync_case_card(self, db: Session, *, case_id: str, receive_id: str | None = None, receive_id_type: str | None = None) -> FeishuCaseBinding:
        if not settings.feishu_live_enabled:
            raise ValueError("FEISHU_LIVE_DISABLED")
        built = FeishuCaseCardBuilder().build(db, case_id)
        binding = db.scalar(select(FeishuCaseBinding).where(FeishuCaseBinding.case_id == case_id).limit(1))
        rid = receive_id or (binding.receive_id if binding else "") or settings.feishu_default_receive_id
        rtype = receive_id_type or (binding.receive_id_type if binding else "") or settings.feishu_receive_id_type
        if not rid:
            raise ValueError("FEISHU_RECEIVE_ID_NOT_CONFIGURED")
        transport = FeishuLiveTransport()
        if binding and binding.message_id:
            await transport.update_card(message_id=binding.message_id, card=built.card)
            binding.receive_id = rid
            binding.receive_id_type = rtype
            binding.status = "ACTIVE"
            binding.card_version += 1
        else:
            result = await transport.send_card(receive_id=rid, receive_id_type=rtype, card=built.card)
            if binding is None:
                binding = FeishuCaseBinding(case_id=case_id, receive_id=rid, receive_id_type=rtype, message_id=result.message_id, status="ACTIVE", card_version=1)
                db.add(binding)
            else:
                binding.receive_id = rid
                binding.receive_id_type = rtype
                binding.message_id = result.message_id
                binding.status = "ACTIVE"
                binding.card_version += 1
        db.flush()
        return binding
