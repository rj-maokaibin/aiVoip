from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.feishu_governance_models import FeishuDocumentAclBinding
from app.db.models import FeishuCaseBinding
from app.integrations.feishu.transport import FeishuLiveTransport, FeishuTransportError
from app.services.audit import audit


_NORMAL_PERMISSION = "view"
_ADMIN_PERMISSION = "full_access"
_ACL_POLICY_VERSION = "feishu-document-acl-v2"


@dataclass(frozen=True)
class Collaborator:
    member_type: str
    member_id: str
    perm: str


class FeishuDocumentPermissionAdapter:
    """Async adapter around Feishu Drive permission APIs.

    Docx is owned by the application. Business principals are reconciled by the
    ACL service: Case initiator and source-chat members are read-only, while only
    explicitly configured ACL admins receive ``full_access``. Ownership is never
    transferred implicitly.
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
    """Reconcile application-owned Docx permissions to a fixed business policy.

    Policy precedence is intentionally simple and fail-closed:
      * Case initiator: ``view``
      * source chat/group members: ``view``
      * ``FEISHU_DOCUMENT_ACL_ADMIN_OPEN_IDS``: ``full_access``

    If a user belongs to more than one category, the strongest configured role
    wins, so an explicitly configured admin is never downgraded to ``view``.
    """

    def __init__(self, adapter: FeishuDocumentPermissionAdapter | None = None):
        self.adapter = adapter or FeishuDocumentPermissionAdapter()

    @staticmethod
    def _source_binding(db: Session, case_id: str) -> FeishuCaseBinding | None:
        return db.scalar(select(FeishuCaseBinding).where(
            FeishuCaseBinding.case_id == case_id,
            FeishuCaseBinding.receive_id_type == "chat_id",
        ).order_by(FeishuCaseBinding.created_at.desc()).limit(1))

    @staticmethod
    def _admin_open_ids() -> list[str]:
        raw = str(getattr(settings, "feishu_document_acl_admin_open_ids", "") or "")
        return sorted({x.strip() for x in raw.split(",") if x.strip()})

    @staticmethod
    def _initiator_open_id(source: FeishuCaseBinding | None) -> str | None:
        value = str(getattr(source, "source_sender_open_id", "") or "").strip()
        return value or None

    @classmethod
    def _desired_acl_fingerprint(cls, source: FeishuCaseBinding, desired_mode: str) -> str:
        material = {
            "policy_version": _ACL_POLICY_VERSION,
            "tenant_key": str(source.source_tenant_key or ""),
            "chat_id": str(source.receive_id or ""),
            "initiator_open_id": cls._initiator_open_id(source),
            "sync_mode": desired_mode,
            "normal_permission": _NORMAL_PERMISSION,
            "admin_permission": _ADMIN_PERMISSION,
            "admin_open_ids": cls._admin_open_ids(),
            "fallback_enabled": bool(settings.feishu_document_acl_fallback_enabled),
        }
        payload = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def ensure_binding(cls, db: Session, *, case_id: str, document_id: str) -> FeishuDocumentAclBinding:
        source = cls._source_binding(db, case_id)
        if source is None or not source.receive_id:
            raise ValueError("FEISHU_SOURCE_CHAT_BINDING_REQUIRED")
        row = db.scalar(select(FeishuDocumentAclBinding).where(
            FeishuDocumentAclBinding.case_id == case_id,
            FeishuDocumentAclBinding.document_id == document_id,
        ).limit(1))
        desired_mode = settings.feishu_document_acl_mode
        # V2 policy is fixed: stale legacy permission settings cannot elevate
        # ordinary Case initiators or source-chat members above read-only.
        desired_permission = _NORMAL_PERMISSION
        desired_fingerprint = cls._desired_acl_fingerprint(source, desired_mode)
        desired_metadata = {
            "acl_policy_version": _ACL_POLICY_VERSION,
            "desired_acl_fingerprint": desired_fingerprint,
            "desired_initiator_open_id": cls._initiator_open_id(source),
            "desired_admin_count": len(cls._admin_open_ids()),
        }
        if row is None:
            row = FeishuDocumentAclBinding(
                case_id=case_id, document_id=document_id,
                tenant_key=str(source.source_tenant_key or ""), chat_id=source.receive_id,
                sync_mode=desired_mode, desired_permission=desired_permission,
                desired_revision=1, applied_revision=0, status="PENDING",
                metadata_json=desired_metadata,
            )
            db.add(row)
            db.flush()
            return row

        metadata = dict(row.metadata_json or {})
        changed = (
            row.tenant_key != str(source.source_tenant_key or "")
            or row.chat_id != source.receive_id
            or row.sync_mode != desired_mode
            or row.desired_permission != desired_permission
            or metadata.get("desired_acl_fingerprint") != desired_fingerprint
        )
        row.tenant_key = str(source.source_tenant_key or "")
        row.chat_id = source.receive_id
        row.sync_mode = desired_mode
        row.desired_permission = desired_permission
        metadata.update(desired_metadata)
        row.metadata_json = metadata
        if changed:
            row.desired_revision += 1
            row.status = "PENDING"
        db.flush()
        return row

    @staticmethod
    def _desired_direct_permissions(
        initiator_open_id: str | None,
        admin_open_ids: list[str],
    ) -> dict[str, str]:
        desired: dict[str, str] = {}
        if initiator_open_id:
            desired[initiator_open_id] = _NORMAL_PERMISSION
        for open_id in admin_open_ids:
            desired[open_id] = _ADMIN_PERMISSION
        return desired

    async def _sync_openid_permissions(
        self,
        row: FeishuDocumentAclBinding,
        *,
        current: dict[str, Collaborator],
        desired: dict[str, str],
        stale_managed: set[str],
    ) -> tuple[list[str], int]:
        changed: list[str] = []
        admin_change_count = 0
        for open_id in sorted(desired):
            perm = desired[open_id]
            existing = current.get(open_id)
            if existing is None:
                await self.adapter.add_collaborator(
                    row.document_id, member_type="openid", member_id=open_id, perm=perm,
                )
                changed.append(f"ADD:{open_id[:12]}:{perm}")
                if perm == _ADMIN_PERMISSION:
                    admin_change_count += 1
            elif existing.perm != perm:
                await self.adapter.update_collaborator(
                    row.document_id, member_type="openid", member_id=open_id, perm=perm,
                )
                changed.append(f"UPDATE:{open_id[:12]}:{perm}")
                if perm == _ADMIN_PERMISSION:
                    admin_change_count += 1

        for open_id in sorted(stale_managed - set(desired)):
            if open_id not in current:
                continue
            await self.adapter.remove_collaborator(
                row.document_id, member_type="openid", member_id=open_id,
            )
            changed.append(f"REMOVE:{open_id[:12]}")
        return changed, admin_change_count

    async def _chat_scope(
        self,
        row: FeishuDocumentAclBinding,
        *,
        initiator_open_id: str | None = None,
        admin_open_ids: list[str] | None = None,
    ) -> dict:
        if not await self.adapter.bot_in_chat(row.chat_id):
            raise FeishuTransportError("FEISHU_BOT_NOT_IN_SOURCE_CHAT")

        admins = self._admin_open_ids() if admin_open_ids is None else sorted(set(admin_open_ids))
        collaborators = await self.adapter.list_collaborators(row.document_id)
        current_chat = next((item for item in collaborators
                             if item.member_type == "openchat" and item.member_id == row.chat_id), None)
        changed: list[str] = []
        if current_chat is None:
            await self.adapter.add_collaborator(
                row.document_id, member_type="openchat", member_id=row.chat_id,
                perm=_NORMAL_PERMISSION,
            )
            changed.append("ADD_CHAT:view")
        elif current_chat.perm != _NORMAL_PERMISSION:
            await self.adapter.update_collaborator(
                row.document_id, member_type="openchat", member_id=row.chat_id,
                perm=_NORMAL_PERMISSION,
            )
            changed.append("UPDATE_CHAT:view")

        current_openids = {item.member_id: item for item in collaborators if item.member_type == "openid"}
        desired = self._desired_direct_permissions(initiator_open_id, admins)
        metadata = dict(row.metadata_json or {})
        previous_managed = set(metadata.get("managed_admin_open_ids") or [])
        previous_initiator = str(metadata.get("managed_initiator_open_id") or "").strip()
        if previous_initiator:
            previous_managed.add(previous_initiator)
        direct_changes, admin_change_count = await self._sync_openid_permissions(
            row, current=current_openids, desired=desired, stale_managed=previous_managed,
        )
        changed.extend(direct_changes)
        return {
            "mode": "CHAT_SCOPE",
            "changed": changed,
            "member_count": 1,
            "managed_open_ids": sorted(desired),
            "admin_change_count": admin_change_count,
        }

    async def _member_mirror(
        self,
        row: FeishuDocumentAclBinding,
        *,
        initiator_open_id: str | None = None,
        admin_open_ids: list[str] | None = None,
    ) -> dict:
        admins = self._admin_open_ids() if admin_open_ids is None else sorted(set(admin_open_ids))
        group_members = set(await self.adapter.list_chat_members(row.chat_id))
        desired_ids = set(group_members)
        if initiator_open_id:
            desired_ids.add(initiator_open_id)
        desired_ids.update(admins)
        admin_set = set(admins)
        desired = {
            open_id: (_ADMIN_PERMISSION if open_id in admin_set else _NORMAL_PERMISSION)
            for open_id in desired_ids
        }

        collaborators = await self.adapter.list_collaborators(row.document_id)
        current = {item.member_id: item for item in collaborators if item.member_type == "openid"}
        # MEMBER_MIRROR is intentionally an exact mirror of current source-group
        # membership plus Case initiator and configured admins. Therefore any
        # direct openid collaborator no longer in that desired set is revoked.
        changed, admin_change_count = await self._sync_openid_permissions(
            row, current=current, desired=desired, stale_managed=set(current),
        )
        return {
            "mode": "MEMBER_MIRROR",
            "changed": changed,
            "member_count": len(group_members),
            "managed_open_ids": sorted(desired),
            "admin_change_count": admin_change_count,
        }

    async def _grant_admin_manage(self, row: FeishuDocumentAclBinding) -> list[str]:
        """Backward-compatible helper: configured admins always receive full_access."""
        admins = self._admin_open_ids()
        if not admins:
            return []
        collaborators = await self.adapter.list_collaborators(row.document_id)
        current = {c.member_id: c for c in collaborators if c.member_type == "openid"}
        changed, _ = await self._sync_openid_permissions(
            row,
            current=current,
            desired={open_id: _ADMIN_PERMISSION for open_id in admins},
            stale_managed=set(),
        )
        return changed

    async def reconcile(self, db: Session, *, case_id: str, document_id: str,
                        actor: str = "feishu-document-acl") -> FeishuDocumentAclBinding:
        row = self.ensure_binding(db, case_id=case_id, document_id=document_id)
        if row.status == "SYNCED" and row.applied_revision == row.desired_revision:
            return row

        source = self._source_binding(db, case_id)
        if source is None:
            raise ValueError("FEISHU_SOURCE_CHAT_BINDING_REQUIRED")
        initiator_open_id = self._initiator_open_id(source)
        admin_open_ids = self._admin_open_ids()
        row.status = "SYNCING"
        row.last_error = None
        db.flush()
        requested = row.sync_mode

        try:
            if requested == "MEMBER_MIRROR":
                result = await self._member_mirror(
                    row, initiator_open_id=initiator_open_id, admin_open_ids=admin_open_ids,
                )
            else:
                try:
                    result = await self._chat_scope(
                        row, initiator_open_id=initiator_open_id, admin_open_ids=admin_open_ids,
                    )
                except FeishuTransportError:
                    if requested != "AUTO" or not settings.feishu_document_acl_fallback_enabled:
                        raise
                    result = await self._member_mirror(
                        row, initiator_open_id=initiator_open_id, admin_open_ids=admin_open_ids,
                    )
                    result["fallback_from"] = "CHAT_SCOPE"

            row.effective_mode = result["mode"]
            row.applied_revision = row.desired_revision
            row.status = "SYNCED"
            row.retry_count = 0
            row.last_synced_at = datetime.now(timezone.utc)
            metadata = dict(row.metadata_json or {})
            metadata.update({
                "acl_policy_version": _ACL_POLICY_VERSION,
                "member_count": result.get("member_count", 0),
                "change_count": len(result.get("changed") or []),
                "fallback_from": result.get("fallback_from"),
                "admin_count": len(admin_open_ids),
                "admin_change_count": int(result.get("admin_change_count") or 0),
                "managed_admin_open_ids": admin_open_ids,
                "managed_initiator_open_id": initiator_open_id,
                "managed_open_ids": result.get("managed_open_ids") or [],
            })
            row.metadata_json = metadata
            audit(
                db,
                case_id=case_id,
                actor=actor,
                event_type="FEISHU_DOCUMENT_ACL_SYNCED",
                target_type="feishu_document_acl",
                target_id=row.id,
                detail={
                    "document_id": document_id,
                    "requested_mode": requested,
                    "effective_mode": row.effective_mode,
                    "permission": _NORMAL_PERMISSION,
                    "admin_permission": _ADMIN_PERMISSION,
                    "desired_revision": row.desired_revision,
                    "change_count": len(result.get("changed") or []),
                    "admin_change_count": int(result.get("admin_change_count") or 0),
                },
            )
        except Exception as exc:
            row.retry_count += 1
            row.status = "FAILED"
            row.last_error = f"{type(exc).__name__}:{exc}"[:1000]
            audit(
                db,
                case_id=case_id,
                actor=actor,
                event_type="FEISHU_DOCUMENT_ACL_FAILED",
                target_type="feishu_document_acl",
                target_id=row.id,
                detail={
                    "document_id": document_id,
                    "requested_mode": requested,
                    "retry_count": row.retry_count,
                    "error_code": type(exc).__name__,
                    "error_message": str(exc)[:500],
                },
            )
            raise

        db.flush()
        return row
