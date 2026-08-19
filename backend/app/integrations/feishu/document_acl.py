from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.evidence_report_models import FeishuEvidenceDocumentBinding
from app.db.feishu_governance_models import FeishuDocumentAclBinding
from app.db.models import FeishuCaseBinding
from app.integrations.feishu.transport import FeishuLiveTransport, FeishuTransportError
from app.services.audit import audit


@dataclass(frozen=True)
class Collaborator:
    member_type: str
    member_id: str
    perm: str


class FeishuDocumentPermissionAdapter:
    """Async adapter around Feishu Drive permission APIs.

    Docx is owned by the application; collaborators receive only the configured
    business permission (V1 default: view). The adapter never changes document
    ownership and never grants full_access implicitly.
    """

    def __init__(self, transport: FeishuLiveTransport | None = None):
        self.transport = transport or FeishuLiveTransport()

    async def list_collaborators(self, document_id: str) -> list[Collaborator]:
        data = await self.transport._request(
            "GET", f"/drive/v1/permissions/{quote(document_id, safe='')}/members",
            params={"type": "docx", "fields": "*"},
        )
        rows = (data.get("data") or {}).get("items") or (data.get("data") or {}).get("members") or []
        return [Collaborator(
            member_type=str(row.get("member_type") or ""),
            member_id=str(row.get("member_id") or ""),
            perm=str(row.get("perm") or ""),
        ) for row in rows if row.get("member_id")]

    async def add_collaborator(self, document_id: str, *, member_type: str, member_id: str, perm: str) -> None:
        await self.transport._request(
            "POST", f"/drive/v1/permissions/{quote(document_id, safe='')}/members",
            params={"type": "docx", "need_notification": "false"},
            json_body={"member_type": member_type, "member_id": member_id, "perm": perm,
                       "type": "chat" if member_type == "openchat" else "user"},
        )

    async def update_collaborator(self, document_id: str, *, member_type: str, member_id: str, perm: str) -> None:
        await self.transport._request(
            "PUT", f"/drive/v1/permissions/{quote(document_id, safe='')}/members/{quote(member_id, safe='')}",
            params={"type": "docx", "need_notification": "false"},
            json_body={"member_type": member_type, "perm": perm,
                       "type": "chat" if member_type == "openchat" else "user"},
        )

    async def remove_collaborator(self, document_id: str, *, member_type: str, member_id: str) -> None:
        await self.transport._request(
            "DELETE", f"/drive/v1/permissions/{quote(document_id, safe='')}/members/{quote(member_id, safe='')}",
            params={"type": "docx", "member_type": member_type},
            json_body={"type": "chat" if member_type == "openchat" else "user"},
        )

    async def list_chat_members(self, chat_id: str) -> list[str]:
        result: list[str] = []
        page_token = ""
        while True:
            params = {"member_id_type": "open_id", "page_size": "100"}
            if page_token:
                params["page_token"] = page_token
            data = await self.transport._request(
                "GET", f"/im/v1/chats/{quote(chat_id, safe='')}/members", params=params,
            )
            payload = data.get("data") or {}
            for item in payload.get("items") or []:
                member_id = item.get("member_id") or item.get("open_id")
                if member_id:
                    result.append(str(member_id))
            if not payload.get("has_more"):
                break
            page_token = str(payload.get("page_token") or "")
            if not page_token:
                break
        return list(dict.fromkeys(result))

    async def bot_in_chat(self, chat_id: str) -> bool:
        data = await self.transport._request(
            "GET", f"/im/v1/chats/{quote(chat_id, safe='')}/members/is_in_chat",
        )
        payload = data.get("data") or {}
        return bool(payload.get("is_in_chat"))


class FeishuDocumentAclService:
    def __init__(self, adapter: FeishuDocumentPermissionAdapter | None = None):
        self.adapter = adapter or FeishuDocumentPermissionAdapter()

    @staticmethod
    def _source_binding(db: Session, case_id: str) -> FeishuCaseBinding | None:
        return db.scalar(select(FeishuCaseBinding).where(
            FeishuCaseBinding.case_id == case_id,
            FeishuCaseBinding.receive_id_type == "chat_id",
        ).order_by(FeishuCaseBinding.created_at.desc()).limit(1))

    @staticmethod
    def ensure_binding(db: Session, *, case_id: str, document_id: str) -> FeishuDocumentAclBinding:
        source = FeishuDocumentAclService._source_binding(db, case_id)
        if source is None or not source.receive_id:
            raise ValueError("FEISHU_SOURCE_CHAT_BINDING_REQUIRED")
        row = db.scalar(select(FeishuDocumentAclBinding).where(
            FeishuDocumentAclBinding.case_id == case_id,
            FeishuDocumentAclBinding.document_id == document_id,
        ).limit(1))
        desired_mode = settings.feishu_document_acl_mode
        desired_permission = settings.feishu_document_acl_permission
        if row is None:
            row = FeishuDocumentAclBinding(
                case_id=case_id, document_id=document_id,
                tenant_key=str(source.source_tenant_key or ""), chat_id=source.receive_id,
                sync_mode=desired_mode, desired_permission=desired_permission,
                desired_revision=1, applied_revision=0, status="PENDING",
            )
            db.add(row); db.flush(); return row
        changed = (
            row.tenant_key != str(source.source_tenant_key or "")
            or row.chat_id != source.receive_id
            or row.sync_mode != desired_mode
            or row.desired_permission != desired_permission
        )
        row.tenant_key = str(source.source_tenant_key or "")
        row.chat_id = source.receive_id
        row.sync_mode = desired_mode
        row.desired_permission = desired_permission
        if changed:
            row.desired_revision += 1
            row.status = "PENDING"
        db.flush(); return row

    async def _chat_scope(self, row: FeishuDocumentAclBinding) -> dict:
        if not await self.adapter.bot_in_chat(row.chat_id):
            raise FeishuTransportError("FEISHU_BOT_NOT_IN_SOURCE_CHAT")
        collaborators = await self.adapter.list_collaborators(row.document_id)
        current = next((item for item in collaborators
                        if item.member_type == "openchat" and item.member_id == row.chat_id), None)
        if current is None:
            await self.adapter.add_collaborator(
                row.document_id, member_type="openchat", member_id=row.chat_id,
                perm=row.desired_permission,
            )
            changed = ["ADD_CHAT"]
        elif current.perm != row.desired_permission:
            await self.adapter.update_collaborator(
                row.document_id, member_type="openchat", member_id=row.chat_id,
                perm=row.desired_permission,
            )
            changed = ["UPDATE_CHAT"]
        else:
            changed = []
        return {"mode": "CHAT_SCOPE", "changed": changed, "member_count": 1}

    async def _member_mirror(self, row: FeishuDocumentAclBinding) -> dict:
        desired = set(await self.adapter.list_chat_members(row.chat_id))
        collaborators = await self.adapter.list_collaborators(row.document_id)
        current = {item.member_id: item for item in collaborators if item.member_type == "openid"}
        changed: list[str] = []
        for open_id in sorted(desired):
            existing = current.get(open_id)
            if existing is None:
                await self.adapter.add_collaborator(
                    row.document_id, member_type="openid", member_id=open_id,
                    perm=row.desired_permission,
                ); changed.append(f"ADD:{open_id}")
            elif existing.perm != row.desired_permission:
                await self.adapter.update_collaborator(
                    row.document_id, member_type="openid", member_id=open_id,
                    perm=row.desired_permission,
                ); changed.append(f"UPDATE:{open_id}")
        for open_id in sorted(set(current) - desired):
            await self.adapter.remove_collaborator(
                row.document_id, member_type="openid", member_id=open_id,
            ); changed.append(f"REMOVE:{open_id}")
        return {"mode": "MEMBER_MIRROR", "changed": changed, "member_count": len(desired)}

    async def reconcile(self, db: Session, *, case_id: str, document_id: str,
                        actor: str = "feishu-document-acl") -> FeishuDocumentAclBinding:
        row = self.ensure_binding(db, case_id=case_id, document_id=document_id)
        if row.status == "SYNCED" and row.applied_revision == row.desired_revision:
            return row
        row.status = "SYNCING"; row.last_error = None; db.flush()
        requested = row.sync_mode
        try:
            if requested == "MEMBER_MIRROR":
                result = await self._member_mirror(row)
            else:
                try:
                    result = await self._chat_scope(row)
                except FeishuTransportError:
                    if requested != "AUTO" or not settings.feishu_document_acl_fallback_enabled:
                        raise
                    result = await self._member_mirror(row)
                    result["fallback_from"] = "CHAT_SCOPE"
            row.effective_mode = result["mode"]
            row.applied_revision = row.desired_revision
            row.status = "SYNCED"
            row.retry_count = 0
            row.last_synced_at = datetime.now(timezone.utc)
            row.metadata_json = {
                "member_count": result.get("member_count", 0),
                "change_count": len(result.get("changed") or []),
                "fallback_from": result.get("fallback_from"),
            }
            audit(db, case_id=case_id, actor=actor,
                  event_type="FEISHU_DOCUMENT_ACL_SYNCED",
                  target_type="feishu_document_acl", target_id=row.id,
                  detail={"document_id": document_id, "requested_mode": requested,
                          "effective_mode": row.effective_mode,
                          "permission": row.desired_permission,
                          "desired_revision": row.desired_revision,
                          "change_count": len(result.get("changed") or [])})
        except Exception as exc:
            row.retry_count += 1
            row.status = "FAILED"
            row.last_error = f"{type(exc).__name__}:{exc}"[:1000]
            audit(db, case_id=case_id, actor=actor,
                  event_type="FEISHU_DOCUMENT_ACL_FAILED",
                  target_type="feishu_document_acl", target_id=row.id,
                  detail={"document_id": document_id, "requested_mode": requested,
                          "retry_count": row.retry_count,
                          "error_code": type(exc).__name__, "error_message": str(exc)[:500]})
            raise
        db.flush(); return row
