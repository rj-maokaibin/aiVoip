from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.ids import new_id
from app.db.conversation_models import Conversation, ConversationState, ConversationTurn
from app.db.models import FeishuCaseBinding

_BLOCKING_SLOT_STATES = {"UNKNOWN_BY_USER", "UNAVAILABLE", "DECLINED", "NOT_APPLICABLE"}


def need_from_question_text(text: str) -> str | None:
    lowered = (text or "").lower()
    if "时间" in text or "timestamp" in lowered:
        return "anomaly_timestamp"
    if "pcap" in lowered or "pcapng" in lowered or "抓包" in text:
        return "pcap"
    if "录音" in text or "audio" in lowered:
        return "recording"
    if "复现" in text:
        return "reproducibility"
    if "设备入口" in text or "ip+sn" in lowered or "url" in lowered:
        return "device_access"
    return None


def slot_label(slot_key: str | None) -> str:
    return {
        "anomaly_timestamp": "异常时间",
        "pcap": "新的抓包",
        "recording": "现场录音",
        "reproducibility": "现场复现条件",
        "device_access": "设备入口",
    }.get(str(slot_key or ""), "这项信息")


class ConversationStateService:
    def _binding_context(self, db: Session, case_id: str | None) -> tuple[str, str]:
        if not case_id:
            return "", "unbound"
        binding = db.scalar(
            select(FeishuCaseBinding)
            .where(FeishuCaseBinding.case_id == case_id)
            .order_by(FeishuCaseBinding.created_at.desc())
            .limit(1)
        )
        if binding is None:
            return "", f"case:{case_id}"
        return str(binding.source_tenant_key or ""), str(binding.receive_id or f"case:{case_id}")

    def get_or_create(
        self,
        db: Session,
        *,
        case_id: str | None,
        source_context: dict[str, Any] | None = None,
    ) -> tuple[Conversation, ConversationState]:
        source_context = source_context or {}
        tenant_key = str(source_context.get("tenant_key") or "")
        chat_id = str(source_context.get("chat_id") or "")
        if not chat_id:
            bound_tenant, bound_chat = self._binding_context(db, case_id)
            tenant_key = tenant_key or bound_tenant
            chat_id = bound_chat
        conversation = db.scalar(
            select(Conversation).where(
                Conversation.tenant_key == tenant_key,
                Conversation.channel == "FEISHU",
                Conversation.chat_id == chat_id,
            ).limit(1)
        )
        if conversation is None:
            conversation = Conversation(
                tenant_key=tenant_key,
                channel="FEISHU",
                chat_id=chat_id,
                active_case_id=case_id,
                entities_json={},
                turn_no=0,
                status="ACTIVE",
            )
            db.add(conversation)
            db.flush()
        elif case_id and conversation.active_case_id != case_id:
            conversation.active_case_id = case_id
            db.flush()
        state = db.scalar(
            select(ConversationState).where(
                ConversationState.conversation_id == conversation.id
            ).limit(1)
        )
        if state is None:
            state = ConversationState(
                conversation_id=conversation.id,
                slots_json={},
                unavailable_needs_json=[],
            )
            db.add(state)
            db.flush()
        return conversation, state

    def case_state(self, db: Session, case_id: str) -> tuple[Conversation | None, ConversationState | None]:
        conversation = db.scalar(
            select(Conversation)
            .where(Conversation.active_case_id == case_id, Conversation.status == "ACTIVE")
            .order_by(Conversation.updated_at.desc())
            .limit(1)
        )
        if conversation is None:
            return None, None
        state = db.scalar(
            select(ConversationState).where(ConversationState.conversation_id == conversation.id).limit(1)
        )
        return conversation, state

    def mark_question_asked(
        self,
        db: Session,
        *,
        case_id: str,
        text: str,
        need: str | None = None,
    ) -> dict[str, Any]:
        conversation, state = self.get_or_create(db, case_id=case_id)
        slot_key = need or need_from_question_text(text)
        slots = dict(state.slots_json or {})
        prior = dict(slots.get(slot_key) or {}) if slot_key else {}
        if slot_key and str(prior.get("state") or "") in _BLOCKING_SLOT_STATES:
            return {
                "should_ask": False,
                "reason": "SLOT_ALREADY_UNAVAILABLE",
                "slot_key": slot_key,
                "slot_state": prior.get("state"),
                "conversation_id": conversation.id,
            }
        asked_count = int(prior.get("asked_count") or 0) + 1
        question_id = hashlib.sha256(
            f"{case_id}\x1f{slot_key or 'generic'}\x1f{text.strip()}".encode("utf-8")
        ).hexdigest()[:24]
        question = {
            "id": question_id,
            "slot_key": slot_key,
            "need": need or slot_key,
            "text": text[:1000],
            "asked_count": asked_count,
            "state": "ASKED",
        }
        state.active_question_json = question
        if slot_key:
            slots[slot_key] = {
                **prior,
                "state": "ASKED",
                "asked_count": asked_count,
                "last_question_id": question_id,
            }
            state.slots_json = slots
        db.flush()
        return {
            "should_ask": True,
            "slot_key": slot_key,
            "question_id": question_id,
            "asked_count": asked_count,
            "conversation_id": conversation.id,
        }

    def apply_turn_interpretation(
        self,
        db: Session,
        *,
        case_id: str | None,
        source_context: dict[str, Any] | None,
        interpretation: dict[str, Any],
    ) -> tuple[Conversation, ConversationState]:
        conversation, state = self.get_or_create(db, case_id=case_id, source_context=source_context)
        answer = interpretation.get("active_question_answer") or None
        slots = dict(state.slots_json or {})
        if isinstance(answer, dict) and answer.get("slot_key"):
            slot_key = str(answer["slot_key"])
            previous = dict(slots.get(slot_key) or {})
            new_state = str(answer.get("state") or "ANSWERED")
            slots[slot_key] = {
                **previous,
                "state": new_state,
                "value": answer.get("value"),
                "confidence": answer.get("confidence"),
            }
            state.slots_json = slots
            unavailable = set(str(x) for x in (state.unavailable_needs_json or []))
            if new_state in _BLOCKING_SLOT_STATES:
                unavailable.add(slot_key)
            else:
                unavailable.discard(slot_key)
            state.unavailable_needs_json = sorted(unavailable)
            active = dict(state.active_question_json or {})
            if active.get("slot_key") == slot_key:
                state.active_question_json = None
        entities = interpretation.get("entities") or {}
        if isinstance(entities, dict) and entities:
            current = dict(conversation.entities_json or {})
            for key, value in entities.items():
                if value not in (None, "", [], {}):
                    current[str(key)[:64]] = value
            conversation.entities_json = current
        state.last_user_intent = str(interpretation.get("intent") or "")[:64] or None
        if interpretation.get("material_diagnostic_context"):
            material = json.dumps(
                {
                    "case_id": case_id,
                    "slots": state.slots_json or {},
                    "entities": conversation.entities_json or {},
                    "turn": interpretation,
                },
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            )
            state.material_context_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
        db.flush()
        return conversation, state

    def record_user_turn(
        self,
        db: Session,
        *,
        case_id: str | None,
        source_context: dict[str, Any] | None,
        text: str,
        interpretation: dict[str, Any],
        model_name: str | None = None,
        prompt_version: str | None = None,
        snapshot_hash: str | None = None,
    ) -> ConversationTurn:
        source_context = source_context or {}
        conversation, _state = self.apply_turn_interpretation(
            db,
            case_id=case_id,
            source_context=source_context,
            interpretation=interpretation,
        )
        message_id = str(source_context.get("message_id") or "") or None
        if message_id:
            existing = db.scalar(
                select(ConversationTurn).where(
                    ConversationTurn.conversation_id == conversation.id,
                    ConversationTurn.source_message_id == message_id,
                ).limit(1)
            )
            if existing is not None:
                return existing
        conversation.turn_no = int(conversation.turn_no or 0) + 1
        row = ConversationTurn(
            id=new_id(),
            conversation_id=conversation.id,
            case_id=case_id,
            source_message_id=message_id,
            sender_id=str(source_context.get("sender_open_id") or "") or None,
            direction="USER",
            text=text,
            intent=str(interpretation.get("intent") or "GENERAL_CHAT"),
            classification=str(interpretation.get("classification") or "CHAT_ONLY"),
            route_mode=str(interpretation.get("route_mode") or "CASE_CHAT"),
            material_diagnostic_context=bool(interpretation.get("material_diagnostic_context")),
            parsed_json=interpretation,
            model_name=model_name,
            prompt_version=prompt_version,
            snapshot_hash=snapshot_hash,
        )
        db.add(row)
        db.flush()
        return row

    def attach_evidence(self, db: Session, *, turn_id: str, evidence_id: str) -> None:
        turn = db.get(ConversationTurn, turn_id)
        if turn is not None:
            turn.evidence_id = evidence_id
            db.flush()

    def semantic_feedback_key(self, db: Session, *, case_id: str, text: str) -> str:
        conversation, state = self.get_or_create(db, case_id=case_id)
        payload = {
            "case_id": case_id,
            "text": text,
            "active_question": state.active_question_json,
            "slots": state.slots_json,
            "unavailable": state.unavailable_needs_json,
            "material_hash": state.material_context_hash,
        }
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
