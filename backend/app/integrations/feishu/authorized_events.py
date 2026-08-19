from __future__ import annotations

import hashlib

from sqlalchemy.orm import Session

from app.api.feishu_permissions import (
    FeishuAuthorizationDecision,
    FeishuCapability,
    authorize_capability,
    authorize_intent,
)
from app.core.config import settings
from app.core.errors import AppError
from app.db.ai_intelligence_models import AIDiagnosticCycle
from app.db.models import DiagnosticExperiment, ReproductionSession
from app.integrations.feishu.case_resolver import resolve_case
from app.integrations.feishu.events import CARD_ACTION_EVENT_TYPES, action_value, dispatch_event
from app.integrations.feishu.feedback import enqueue_reply
from app.integrations.feishu.identity import resolve_feishu_identity
from app.integrations.feishu.intake import extract_message_content, route_intake
from app.integrations.feishu.semantic_router import shadow_semantic_route
from app.services.audit import audit
from app.services.idempotency import begin_idempotent, complete_idempotent

_CARD_CAPABILITIES = {
    "STOP_REPRODUCTION": FeishuCapability.CONTROL_REPRODUCTION,
    "EXTERNAL_ACTION_COMPLETED": FeishuCapability.COMPLETE_EXTERNAL_ACTION,
    "OPEN_CASE": FeishuCapability.VIEW_CASE,
    "AI2_ACCEPT_SUGGESTION": FeishuCapability.RUN_AI_SUGGESTION,
}
_DENIED_MESSAGE = "当前飞书账号未完成权限映射、已被禁用，或没有执行该操作的权限。请联系 VOIP AI 管理员。"


def _operator_open_id(payload: dict) -> str:
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    candidates = [payload.get("operator"), event.get("operator") if isinstance(event, dict) else None, event.get("sender") if isinstance(event, dict) else None]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        sender_id = candidate.get("sender_id") if isinstance(candidate.get("sender_id"), dict) else candidate
        if sender_id.get("open_id"):
            return str(sender_id["open_id"])
    return ""


def _tenant_key(payload: dict) -> str:
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    return str(header.get("tenant_key") or sender.get("tenant_key") or "")


def _case_copilot_idempotency_key(*, tenant_key: str, case_id: str, actor_id: str, actor_role: str, delivery_id: str) -> str:
    """Scope AI3 replay protection to the effective authorization context."""
    material = "\x1f".join((tenant_key, case_id, actor_id, actor_role, delivery_id))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _audit_identity_resolution(db: Session, identity, *, open_id: str, case_id: str | None = None) -> None:
    digest = hashlib.sha256(str(open_id or "").encode("utf-8")).hexdigest()[:20] if open_id else None
    audit(db, case_id=case_id, actor=identity.actor_id or "feishu-identity-resolver", event_type="FEISHU_IDENTITY_RESOLVED", target_type="feishu_user_identity", target_id=identity.identity_id, detail={
        "schema_version": "feishu-identity-resolution-v1", "tenant_key": identity.tenant_key,
        "open_id_hash": digest, "actor_id": identity.actor_id,
        "role": identity.role.value if identity.role else None, "status": identity.status,
        "resolution_source": identity.resolution_source,
    })


def _permission_denied_result(*, decision: FeishuAuthorizationDecision | None, identity_status: str, card_action: bool = False) -> dict:
    capability = decision.capability.value if decision and decision.capability else None
    reason = decision.reason if decision else "IDENTITY_NOT_ACTIVE"
    result = {"handled": "permission_denied", "reason": reason, "capability": capability, "identity_status": identity_status}
    if card_action:
        result["toast"] = {"type": "error", "content": "当前飞书账号没有执行该操作的权限，请联系 Case 负责人或管理员。"}
    return result


def _persist_message_denial(db: Session, *, payload: dict, result: dict, message_id: str, event_id: str) -> dict:
    """Idempotently persist a denial and reply exactly once."""
    key = message_id or event_id or None
    if not key:
        if message_id:
            enqueue_reply(message_id, _DENIED_MESSAGE)
        return result
    try:
        handle = begin_idempotent(db, scope="FEISHU_RBAC_DENIED_EVENT", key=key, payload={
            "event_type": ((payload.get("header") or {}).get("event_type") if isinstance(payload.get("header"), dict) else None),
            "message_id": message_id, "event_id": event_id, "reason": result.get("reason"), "capability": result.get("capability"),
        })
    except AppError as exc:
        if exc.code == "IDEMPOTENCY_IN_PROGRESS":
            return {**result, "duplicate": True}
        raise
    if handle.replay is not None:
        return {**handle.replay, "duplicate": True}
    if message_id:
        enqueue_reply(message_id, _DENIED_MESSAGE)
    complete_idempotent(db, handle, response=result, status_code=200, resource_type="feishu_authorization_denial", resource_id=message_id or event_id or None)
    return result


def _authorize_message(db: Session, payload: dict):
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    tenant = _tenant_key(payload)
    open_id = _operator_open_id(payload)
    identity = resolve_feishu_identity(db, tenant_key=tenant, open_id=open_id, discover_unmapped=settings.feishu_identity_discover_unmapped)
    event_id = str(header.get("event_id") or "")
    message_id = str(message.get("message_id") or "")
    _audit_identity_resolution(db, identity, open_id=open_id)
    if not identity.active:
        result = _permission_denied_result(decision=None, identity_status=identity.status)
        return identity, None, _persist_message_denial(db, payload=payload, result=result, message_id=message_id, event_id=event_id)

    text_value, attachments = extract_message_content(message)
    preliminary = route_intake(text=text_value, attachments=attachments, has_thread_case=False)
    chat_id = str(event.get("chat_id") or message.get("chat_id") or "")
    resolution = resolve_case(
        db, tenant_key=tenant, chat_id=chat_id, case_ref=preliminary.case_ref,
        message_id=message_id,
        root_message_id=str(message.get("root_id") or message.get("root_message_id") or ""),
        parent_message_id=str(message.get("parent_id") or message.get("parent_message_id") or ""),
        device_refs=preliminary.device_refs, symptoms=preliminary.symptoms,
    )
    case = resolution.case
    final_intake = route_intake(text=text_value, attachments=attachments, has_thread_case=case is not None)

    if message_id:
        shadow_semantic_route(
            db,
            message_id=message_id,
            text=text_value,
            attachments=attachments,
            deterministic=final_intake,
            case_id=case.id if case else None,
            case_no=case.case_no if case else None,
            tenant_key=tenant or None,
            chat_id=chat_id or None,
        )

    decision = authorize_intent(db, identity=identity, intent=final_intake.intent, case_id=case.id if case else None)
    if not decision.allowed:
        result = _permission_denied_result(decision=decision, identity_status=identity.status)
        return identity, decision, _persist_message_denial(db, payload=payload, result=result, message_id=message_id, event_id=event_id)
    return identity, decision, None


def _maybe_dispatch_case_copilot(db: Session, *, payload: dict, identity) -> dict | None:
    """Handle an authorized, Case-bound GENERAL_QUESTION with AI3."""
    if not settings.ai_case_copilot_enabled or not identity.active or identity.role is None:
        return None
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    message_id = str(message.get("message_id") or "")
    event_id = str(header.get("event_id") or "")
    text, attachments = extract_message_content(message)
    if attachments:
        return None
    tenant = _tenant_key(payload)
    chat_id = str(event.get("chat_id") or message.get("chat_id") or "")
    preliminary = route_intake(text=text, attachments=[], has_thread_case=False)
    resolution = resolve_case(
        db,
        tenant_key=tenant,
        chat_id=chat_id,
        case_ref=preliminary.case_ref,
        message_id=message_id,
        root_message_id=str(message.get("root_id") or message.get("root_message_id") or ""),
        parent_message_id=str(message.get("parent_id") or message.get("parent_message_id") or ""),
        device_refs=preliminary.device_refs, symptoms=preliminary.symptoms,
    )
    case = resolution.case
    if case is None:
        return None
    intake = route_intake(text=text, attachments=[], has_thread_case=True)
    if intake.intent != "GENERAL_QUESTION":
        return None

    delivery_id = message_id or event_id or hashlib.sha256(text.encode()).hexdigest()
    scoped_key = _case_copilot_idempotency_key(
        tenant_key=tenant,
        case_id=case.id,
        actor_id=identity.actor_id,
        actor_role=identity.role.value,
        delivery_id=delivery_id,
    )
    try:
        handle = begin_idempotent(
            db,
            scope="FEISHU_CASE_COPILOT_EVENT",
            key=scoped_key,
            payload={
                "case_id": case.id,
                "message_id": message_id,
                "event_id": event_id,
                "tenant_key": tenant,
                "actor_id": identity.actor_id,
                "actor_role": identity.role.value,
                "text_hash": hashlib.sha256(text.encode()).hexdigest(),
            },
        )
    except AppError as exc:
        if exc.code == "IDEMPOTENCY_IN_PROGRESS":
            return {"handled": "case_copilot", "case_id": case.id, "duplicate": True}
        raise
    if handle.replay is not None:
        return {**handle.replay, "duplicate": True}

    from app.copilot.service import CaseCopilotService

    try:
        with db.begin_nested():
            answer = CaseCopilotService().answer(
                db,
                case_id=case.id,
                question=text,
                request_key=f"feishu:{scoped_key}",
                actor_id=identity.actor_id,
                actor_role=identity.role,
            )
    except Exception as exc:
        error_code = type(exc).__name__[:128]
        audit(
            db,
            case_id=case.id,
            actor=identity.actor_id,
            event_type="AI_CASE_COPILOT_RUNTIME_FAILED",
            target_type="ai_case_copilot",
            target_id=None,
            detail={
                "schema_version": "ai-case-copilot-runtime-failure-v1",
                "error_code": error_code,
                "read_only": True,
                "parent_transaction_preserved": True,
            },
        )
        reply = "Case Copilot 当前不可用；确定性诊断和当前证据未受影响，请查看 Case 主卡或稍后重试。"
        if message_id:
            enqueue_reply(message_id, reply)
        result = {
            "handled": "case_copilot",
            "case_id": case.id,
            "case_no": case.case_no,
            "copilot_status": "RUNTIME_FAILED",
            "copilot_record_id": None,
            "routed_control_intent": None,
            "read_only": True,
            "error_code": error_code,
        }
        complete_idempotent(
            db,
            handle,
            response=result,
            status_code=200,
            resource_type="ai_case_copilot_runtime_failure",
            resource_id=case.id,
        )
        return result

    if answer.status == "ANSWERED":
        reply = answer.answer
    elif answer.status == "CONTROL_INTENT_REQUIRED":
        reply = answer.answer
    elif answer.status == "GATEWAY_FAILED":
        reply = "Case Copilot 当前不可用；确定性诊断和当前证据未受影响，请查看 Case 主卡或稍后重试。"
    else:
        reply = "本次 AI 回答未通过当前 Case 证据约束，因此没有返回该回答。请查看确定性报告或补充证据。"
    if message_id:
        enqueue_reply(message_id, reply)
    result = {
        "handled": "case_copilot",
        "case_id": case.id,
        "case_no": case.case_no,
        "copilot_status": answer.status,
        "copilot_record_id": answer.record_id,
        "routed_control_intent": answer.routed_control_intent,
        "read_only": True,
    }
    complete_idempotent(
        db,
        handle,
        response=result,
        status_code=200,
        resource_type="ai_case_copilot",
        resource_id=answer.record_id,
    )
    return result


def _ai2_underlying_capability(db: Session, value: dict) -> tuple[str | None, FeishuCapability | None]:
    cycle = db.get(AIDiagnosticCycle, str(value.get("cycle_id") or ""))
    if not cycle:
        return str(value.get("case_id") or "") or None, None
    kind = str((cycle.next_action_json or {}).get("type") or "")
    capability = {
        "QUESTION": FeishuCapability.ADD_EVIDENCE,
        "USER_EVIDENCE_REQUEST": FeishuCapability.ADD_EVIDENCE,
        "REPRODUCTION_PROFILE": FeishuCapability.CONTROL_REPRODUCTION,
        "EXPERIMENT_PROFILE": FeishuCapability.RUN_REGISTERED_EXPERIMENT,
    }.get(kind)
    return cycle.case_id, capability


def _card_case_and_capability(db: Session, payload: dict):
    value = action_value(payload)
    action = str(value.get("action") or "").upper()
    capability = _CARD_CAPABILITIES.get(action)
    case_id = str(value.get("case_id") or "") or None
    if action == "STOP_REPRODUCTION":
        row = db.get(ReproductionSession, str(value.get("session_id") or ""))
        case_id = row.case_id if row else case_id
    elif action == "EXTERNAL_ACTION_COMPLETED":
        row = db.get(DiagnosticExperiment, str(value.get("experiment_id") or ""))
        case_id = row.case_id if row else case_id
    elif action == "AI2_ACCEPT_SUGGESTION":
        case_id, _ = _ai2_underlying_capability(db, value)
    return action, case_id, capability


def _authorize_card_action(db: Session, payload: dict):
    open_id = _operator_open_id(payload)
    identity = resolve_feishu_identity(db, tenant_key=_tenant_key(payload), open_id=open_id, discover_unmapped=settings.feishu_identity_discover_unmapped)
    action, case_id, capability = _card_case_and_capability(db, payload)
    _audit_identity_resolution(db, identity, open_id=open_id, case_id=case_id)
    if not identity.active:
        return identity, _permission_denied_result(decision=None, identity_status=identity.status, card_action=True)
    if capability is None:
        return identity, None
    decision = authorize_capability(db, identity=identity, capability=capability, case_id=case_id)
    if not decision.allowed:
        return identity, _permission_denied_result(decision=decision, identity_status=identity.status, card_action=True)
    if action == "AI2_ACCEPT_SUGGESTION":
        value = action_value(payload)
        _, underlying = _ai2_underlying_capability(db, value)
        if underlying is None:
            return identity, _permission_denied_result(
                decision=FeishuAuthorizationDecision(
                    False, FeishuCapability.RUN_AI_SUGGESTION, identity.actor_id,
                    identity.role.value if identity.role else None, identity.status,
                    case_id, None, False, "AI2_SUGGESTION_NOT_ACTIONABLE",
                ),
                identity_status=identity.status,
                card_action=True,
            )
        underlying_decision = authorize_capability(
            db,
            identity=identity,
            capability=underlying,
            case_id=case_id,
        )
        if not underlying_decision.allowed:
            return identity, _permission_denied_result(
                decision=underlying_decision,
                identity_status=identity.status,
                card_action=True,
            )
    return identity, None


def dispatch_authorized_event(db: Session, *, payload: dict, actor: str = "feishu:callback") -> dict:
    """Feishu entry gateway: Identity/RBAC first, optional read-only AI, then business handler."""
    if not settings.feishu_identity_rbac_enabled:
        return dispatch_event(db, payload=payload, actor=actor)

    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event_type = str(header.get("event_type") or payload.get("type") or "")
    if event_type == "im.message.receive_v1":
        identity, _decision, denied = _authorize_message(db, payload)
        if denied is not None:
            return denied
        copilot = _maybe_dispatch_case_copilot(db, payload=payload, identity=identity)
        if copilot is not None:
            return copilot
        return dispatch_event(db, payload=payload, actor=identity.actor_id or actor)

    if event_type in CARD_ACTION_EVENT_TYPES:
        identity, denied = _authorize_card_action(db, payload)
        if denied is not None:
            return denied
        return dispatch_event(db, payload=payload, actor=identity.actor_id or actor)

    return dispatch_event(db, payload=payload, actor=actor)
