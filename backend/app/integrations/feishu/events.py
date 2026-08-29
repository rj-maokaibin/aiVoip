"""Shared Feishu event dispatch used by both the HTTP callback and the WebSocket
long-connection listener.

Both transports normalize an incoming payload into the same header/event shape
and call dispatch_event, so provision / stop-reproduction / experiment actions
behave identically no matter how the event arrived (webhook vs long connection
-- the latter is used when the deployment has no public callback URL).
"""
from __future__ import annotations

from dataclasses import replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AppError
from app.db.models import (
    DiagnosticExperiment, ExperimentRun, Hypothesis, ReproductionCall, ReproductionSession,
)
from app.experiments.orchestrator import DiagnosticExperimentOrchestrator
from app.integrations.feishu.case_resolver import is_explicit_new_fault, resolve_case
from app.integrations.feishu.intake import extract_message_content, route_intake
from app.integrations.feishu.feedback import accepted_text, enqueue_reply
from app.services.idempotency import begin_idempotent, complete_idempotent
from app.workers.reproduction_tasks import cancel_reproduction, start_reproduction


def action_value(payload: dict) -> dict:
    """Extract the action value dict (card button callbacks)."""
    candidates = [
        payload.get("action"),
        (payload.get("event") or {}).get("action") if isinstance(payload.get("event"), dict) else None,
    ]
    for item in candidates:
        if isinstance(item, dict):
            value = item.get("value")
            if isinstance(value, dict):
                return value
    return {}


def callback_actor(payload: dict) -> str:
    for node in [payload.get("operator"),
                 (payload.get("event") or {}).get("operator") if isinstance(payload.get("event"), dict) else None]:
        if isinstance(node, dict):
            for key in ("open_id", "user_id", "union_id"):
                if node.get(key):
                    return f"feishu:{node[key]}"
    return "feishu:callback"


CARD_ACTION_EVENT_TYPES = {"card.action.trigger", "card.action.trigger_v1"}


def _reply_intake(message_id: str, text: str) -> None:
    enqueue_reply(message_id, text)


def _card_action_response(result: dict) -> dict:
    handled = result.get("handled")
    if handled == "error":
        toast = {"type": "error", "content": str(result.get("message") or "操作失败：请稍后重试")}
    elif handled == "stop_reproduction":
        toast = {"type": "info", "content": "已请求安全停止自动复现"}
    elif handled == "external_action_completed":
        toast = {"type": "success", "content": "已记录现场操作完成"}
    elif handled == "ai2_suggestion_accepted":
        toast = {"type": "success", "content": str(result.get("message") or "已采纳 AI2 建议")}
    elif handled == "open_case":
        toast = {"type": "info", "content": "请在网页端查看 Case 详情"}
    else:
        toast = {"type": "info", "content": "已收到操作请求"}
    out = {"handled": handled, "toast": toast}
    if handled in {"stop_reproduction", "external_action_completed", "ai2_suggestion_accepted"}:
        out["updated_card"] = True
    if result.get("reason"):
        out["reason"] = result["reason"]
    if result.get("execution_ref_type"):
        out["execution_ref_type"] = result["execution_ref_type"]
        out["execution_ref_id"] = result.get("execution_ref_id")
    if result.get("idempotent_replay"):
        out["idempotent_replay"] = True
    return out


def _fix_action_type(text: str) -> str:
    lowered = text.lower()
    mappings = (
        (('升级', '补丁', '版本', 'patch'), 'SOFTWARE_PATCH'),
        (('配置', '参数'), 'CONFIG_CHANGE'),
        (('换话机', '更换话机'), 'PHONE_REPLACE'),
        (('换线', '更换线路'), 'LINE_REPLACE'),
        (('换端口', '更换端口'), 'FXS_PORT_CHANGE'),
        (('换设备', '更换设备'), 'DEVICE_REPLACE'),
        (('重启', 'reboot'), 'REBOOT'),
    )
    for words, action_type in mappings:
        if any(word in lowered for word in words):
            return action_type
    return 'OTHER'


def _complete_message(db: Session, handle, result: dict, message_id: str, event_id: str) -> dict:
    complete_idempotent(
        db, handle, response=result, status_code=200,
        resource_type='feishu_message', resource_id=message_id or event_id or None,
    )
    return result


def _dispatch_case_conversation(*, case_id: str, text: str, source_context: dict) -> None:
    """Single asynchronous entry for active-Case conversational turns.

    Status/completion/knowledge/follow-up text all pass through the same interpreter
    so ConversationTurn is persisted before any decision to create technical
    Evidence. The callback itself sends no zero-information acknowledgement.
    """
    from app.workers.device_provision_task import ingest_feishu_follow_up
    ingest_feishu_follow_up.apply_async(args=[case_id, text, source_context], queue='diagnosis')


def dispatch_event(db: Session, *, payload: dict, actor: str = "feishu:callback") -> dict:
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event_type = str(header.get("event_type") or payload.get("type") or "")

    if event_type == "im.message.receive_v1":
        event = payload.get("event") or {}
        msg = event.get("message") or {}
        chat_id = str(event.get("chat_id") or msg.get("chat_id") or "")
        chat_type = str(event.get("chat_type") or msg.get("chat_type") or "")
        text, attachments = extract_message_content(msg)
        if not text and not attachments:
            return {"handled": "empty_text"}
        event_id = str(header.get('event_id') or '')
        message_id = str(msg.get('message_id') or '')
        root_message_id = str(msg.get('root_id') or msg.get('root_message_id') or '')
        parent_message_id = str(msg.get('parent_id') or msg.get('parent_message_id') or '')
        sender = event.get('sender') if isinstance(event.get('sender'), dict) else {}
        sender_id = sender.get('sender_id') if isinstance(sender.get('sender_id'), dict) else {}
        operator = payload.get('operator') if isinstance(payload.get('operator'), dict) else {}
        sender_open_id = str(sender_id.get('open_id') or operator.get('open_id') or '')
        tenant_key = str(header.get('tenant_key') or sender.get('tenant_key') or '')
        source_context = {
            'tenant_key': tenant_key or None,
            'chat_id': chat_id or None,
            'event_id': event_id or None,
            'message_id': message_id or None,
            'root_message_id': root_message_id or None,
            'parent_message_id': parent_message_id or None,
            'sender_open_id': sender_open_id or None,
            'chat_type': chat_type or None,
            'create_time': str(msg.get('create_time') or header.get('create_time') or '') or None,
            'normalized_text': text,
            'attachments': attachments,
        }
        idempotency_key = message_id or event_id or None
        semantic_payload = {
            'event_type': event_type, 'tenant_key': tenant_key,
            'chat_id': chat_id, 'chat_type': chat_type,
            'message_id': message_id, 'root_message_id': root_message_id,
            'parent_message_id': parent_message_id, 'text': text,
            'attachments': attachments,
        }
        try:
            handle = begin_idempotent(
                db, scope='FEISHU_MESSAGE_EVENT', key=idempotency_key,
                payload=semantic_payload,
            )
        except AppError as exc:
            if exc.code == 'IDEMPOTENCY_IN_PROGRESS':
                return {'handled': 'duplicate_in_progress', 'event_id': event_id,
                        'message_id': message_id}
            raise
        if handle.replay is not None:
            return {**handle.replay, 'duplicate': True}

        preliminary = route_intake(text=text, attachments=attachments, has_thread_case=False)
        resolution = resolve_case(
            db, tenant_key=tenant_key, chat_id=chat_id,
            case_ref=preliminary.case_ref, message_id=message_id,
            root_message_id=root_message_id, parent_message_id=parent_message_id,
            device_refs=preliminary.device_refs, symptoms=preliminary.symptoms,
        )
        case = resolution.case
        ambiguous_cases = resolution.ambiguous_cases
        correlation_reason = resolution.reason

        if correlation_reason == 'EXPLICIT_CASE_NOT_FOUND':
            result = {
                'chat_id': chat_id, 'chat_type': chat_type,
                'event_id': event_id or None, 'message_id': message_id or None,
                'intent': preliminary.intent, 'intake': preliminary.to_dict(),
                'case_id': None, 'correlation_reason': correlation_reason,
                'handled': 'needs_clarification',
                'missing_user_inputs': ['valid_case_reference'],
            }
            _reply_intake(message_id, f'未找到你指定的 Case：{preliminary.case_ref}。请确认 Case 编号后重试。')
            return _complete_message(db, handle, result, message_id, event_id)

        intake = route_intake(text=text, attachments=attachments, has_thread_case=case is not None)

        if case and correlation_reason == 'CHAT_ACTIVE_CASE' and intake.intent == 'NEW_DIAGNOSIS':
            if is_explicit_new_fault(text):
                result = {
                    'chat_id': chat_id, 'chat_type': chat_type,
                    'event_id': event_id or None, 'message_id': message_id or None,
                    'intent': intake.intent, 'intake': intake.to_dict(),
                    'case_id': case.id, 'case_no': case.case_no,
                    'correlation_reason': correlation_reason,
                    'handled': 'active_case_conflict',
                    'missing_user_inputs': ['new_group_or_admin_rebind'],
                }
                _reply_intake(
                    message_id,
                    f'当前群已绑定 Active Case {case.case_no}。如果这是新的独立故障，请新建故障群；'
                    '如果是当前 Case 的补充，请直接继续提交现象或附件。',
                )
                return _complete_message(db, handle, result, message_id, event_id)
            intake = replace(
                intake, intent='CASE_FOLLOW_UP', confidence=max(intake.confidence, 0.95),
                missing_user_inputs=[], requires_device_access=False,
                reason='active_chat_case_follow_up',
            )

        base = {
            'chat_id': chat_id, 'chat_type': chat_type,
            'event_id': event_id or None, 'message_id': message_id or None,
            'intent': intake.intent, 'intake': intake.to_dict(),
            'case_id': case.id if case else None,
            'correlation_reason': correlation_reason,
        }

        if ambiguous_cases:
            case_nos = [row.case_no for row in ambiguous_cases[:3]]
            result = {**base, 'handled': 'needs_case_disambiguation',
                      'candidate_case_nos': case_nos,
                      'missing_user_inputs': ['explicit_case_reference']}
            _reply_intake(message_id, f'找到多个可能的 Case：{" / ".join(case_nos)}。请回复具体 Case 编号。')
            return _complete_message(db, handle, result, message_id, event_id)

        if case and correlation_reason in {'DEVICE_SYMPTOM_TIME_WINDOW', 'CHAT_ACTIVE_CASE'}:
            from app.services.audit import audit
            audit(
                db, case_id=case.id, actor=actor, event_type='FEISHU_CASE_CORRELATED',
                target_type='case', target_id=case.id,
                detail={
                    'reason': correlation_reason, 'message_id': message_id,
                    'tenant_key': tenant_key, 'chat_id': chat_id,
                    'device_refs': preliminary.device_refs,
                    'symptoms': preliminary.symptoms,
                },
            )
        if case:
            source_context['correlated_case_id'] = case.id

        if intake.intent == 'STOP_REPRODUCTION':
            query = select(ReproductionSession).where(
                ReproductionSession.state.not_in(['COMPLETED', 'PARTIAL_SUCCESS', 'CANCELLED', 'FAILED'])
            )
            if case:
                query = query.where(ReproductionSession.case_id == case.id)
            session = db.scalar(query.order_by(ReproductionSession.created_at.desc()).limit(1)) if case else None
            if session:
                cancel_reproduction.apply_async(args=[session.id], queue='reproduction-control-high')
                result = {**base, 'handled': 'stop_reproduction', 'session_id': session.id}
                _reply_intake(message_id, '已请求安全停止当前复现任务。')
            else:
                result = {**base, 'handled': 'needs_clarification',
                          'missing_user_inputs': ['case_reference_or_reply_in_case_thread']}
                _reply_intake(message_id, '没有定位到要停止的任务，请回复对应 Case 主卡或提供 Case 编号。')
        elif intake.intent == 'STATUS_QUERY':
            if case:
                _dispatch_case_conversation(case_id=case.id, text=text, source_context=source_context)
                result = {**base, 'handled': 'case_conversation_dispatched',
                          'case_no': case.case_no, 'conversation_kind': 'STATUS_QUERY'}
            else:
                result = {**base, 'handled': 'needs_clarification',
                          'missing_user_inputs': intake.missing_user_inputs}
                _reply_intake(message_id, '请回复对应 Case 主卡，或提供 Case 编号。')
        elif intake.intent == 'EXTERNAL_ACTION_COMPLETED':
            if not case:
                result = {**base, 'handled': 'needs_clarification',
                          'missing_user_inputs': ['case_reference_or_reply_in_case_thread']}
                _reply_intake(message_id, '请回复对应 Case 主卡，或提供 Case 编号。')
            else:
                waiting = list(db.scalars(
                    select(ExperimentRun).where(
                        ExperimentRun.case_id == case.id,
                        ExperimentRun.status == 'WAITING_EXTERNAL_ACTION',
                    ).order_by(ExperimentRun.created_at.desc())
                ))
                if len(waiting) == 1:
                    run = DiagnosticExperimentOrchestrator().complete_external_action(
                        db, run=waiting[0], actor=actor
                    )
                    result = {**base, 'handled': 'external_action_completed',
                              'experiment_run_id': run.id}
                    _reply_intake(message_id, f'Case {case.case_no} 已记录现场操作完成，正在刷新后续诊断准备状态。')
                    from app.workers.device_provision_task import sync_case_card
                    sync_case_card.apply_async(args=[case.id, 'external_action_completed'], queue='diagnosis')
                elif len(waiting) > 1:
                    result = {**base, 'handled': 'needs_clarification',
                              'missing_user_inputs': ['specific_experiment_card']}
                    _reply_intake(message_id, '当前有多个现场操作在等待确认，请在对应实验卡片中点击“现场操作已完成”。')
                else:
                    result = {**base, 'handled': 'no_waiting_external_action'}
                    _reply_intake(message_id, f'Case {case.case_no} 当前没有等待确认的现场操作。')
        elif intake.intent == 'FIX_APPLIED':
            if not case:
                result = {**base, 'handled': 'needs_clarification',
                          'missing_user_inputs': ['case_reference_or_reply_in_case_thread']}
                _reply_intake(message_id, '请回复对应 Case 主卡，或提供 Case 编号。')
            elif case.status != 'ROOT_CAUSE_CONFIRMED':
                from app.workers.device_provision_task import ingest_feishu_follow_up
                ingest_feishu_follow_up.apply_async(args=[case.id, text, source_context], queue='diagnosis')
                result = {**base, 'handled': 'fix_not_ready',
                          'reason': 'ROOT_CAUSE_CONFIRMATION_REQUIRED'}
                _reply_intake(message_id, f'Case {case.case_no} 尚未确认根因；已将这条修复信息作为诊断补充，暂不启动修复验证。')
            else:
                hypothesis = db.scalar(select(Hypothesis).where(
                    Hypothesis.case_id == case.id, Hypothesis.status == 'CONFIRMED'
                ).order_by(Hypothesis.created_at.desc()).limit(1))
                experiment = db.scalar(select(DiagnosticExperiment).where(
                    DiagnosticExperiment.case_id == case.id,
                    DiagnosticExperiment.causal_state == 'ROOT_CAUSE_CONFIRMED',
                ).order_by(DiagnosticExperiment.created_at.desc()).limit(1))
                if not hypothesis and not experiment:
                    result = {**base, 'handled': 'fix_not_ready',
                              'reason': 'ROOT_CAUSE_REFERENCE_MISSING'}
                    _reply_intake(message_id, f'Case {case.case_no} 状态显示已确认根因，但未找到可引用的根因记录；已停止自动创建修复验证，请由研发检查 Case 证据。')
                    return _complete_message(db, handle, result, message_id, event_id)
                from app.experiments.fix_verification import FixVerificationService
                service = FixVerificationService()
                fix = service.create_fix_action(
                    db, case_id=case.id, action_type=_fix_action_type(text),
                    description=text[:1000], hypothesis_id=hypothesis.id if hypothesis else None,
                    experiment_id=experiment.id if experiment else None,
                    metadata={'source': 'FEISHU_TEXT', 'source_message_id': message_id},
                    actor=actor,
                )
                baseline_call = db.scalar(
                    select(ReproductionCall).join(
                        ReproductionSession, ReproductionSession.id == ReproductionCall.session_id
                    ).where(
                        ReproductionSession.case_id == case.id,
                        ReproductionCall.status == 'ANALYZED',
                    ).order_by(ReproductionCall.started_at.desc()).limit(1)
                )
                findings = list((baseline_call.quick_analysis_json or {}).get('findings') or []) if baseline_call else []
                target = experiment.target_finding if experiment and experiment.target_finding in findings else (findings[0] if findings else None)
                verification = None
                if baseline_call and target:
                    verification = service.create_verification(
                        db, fix_action_id=fix.id,
                        baseline_session_id=baseline_call.session_id,
                        baseline_call_id=baseline_call.id, target_finding=target,
                    )
                result = {**base, 'handled': 'fix_applied', 'fix_action_id': fix.id,
                          'fix_verification_id': verification.id if verification else None}
                if verification:
                    _reply_intake(message_id, f'Case {case.case_no} 已记录修复，并创建修复验证计划；系统将在主卡中提示下一次验证操作。')
                    from app.workers.reproduction_tasks import schedule_fix_verification_reproduction
                    schedule_fix_verification_reproduction.apply_async(
                        args=[verification.id], queue='reproduction-control', countdown=1
                    )
                else:
                    _reply_intake(message_id, f'Case {case.case_no} 已记录修复，但缺少可对比的故障基线 Call，暂时无法创建自动验证；请在主卡中查看所需证据。')
                from app.workers.device_provision_task import sync_case_card
                sync_case_card.apply_async(args=[case.id, 'fix_applied'], queue='diagnosis')
        elif intake.intent == 'GENERAL_QUESTION':
            if case:
                _dispatch_case_conversation(case_id=case.id, text=text, source_context=source_context)
                result = {**base, 'handled': 'case_conversation_dispatched',
                          'case_no': case.case_no, 'conversation_kind': 'KNOWLEDGE_OR_CHAT'}
            else:
                from app.knowledge.answering import answer_verified_question
                answer = answer_verified_question(db, text)
                result = {**base, 'handled': 'general_question',
                          'answered': answer['answered'], 'citations': answer['citations']}
                _reply_intake(message_id, answer['text'])
                from app.services.audit import audit
                audit(db, case_id=None, actor=actor,
                      event_type='FEISHU_KNOWLEDGE_ANSWERED', target_type='feishu_message',
                      target_id=message_id or event_id or None,
                      detail={'answered': answer['answered'],
                              'citations': answer['citations'], 'query': text[:500]})
        elif intake.intent == 'NEW_DIAGNOSIS' and intake.missing_user_inputs:
            result = {**base, 'handled': 'needs_clarification',
                      'missing_user_inputs': intake.missing_user_inputs}
            if 'symptom_description' in intake.missing_user_inputs:
                _reply_intake(message_id, '请补充一个用户可感知的现象，例如：单通无声、杂音、按键首位丢失。')
            else:
                _reply_intake(message_id, '请提供设备 URL，或 IP+SN；也可以直接上传 PCAP/PCAPNG 附件。')
        elif intake.intent == 'NEW_DIAGNOSIS' and attachments:
            from app.workers.device_provision_task import ingest_feishu_attachments
            ingest_feishu_attachments.apply_async(
                args=[text, chat_id, chat_type, source_context, attachments], queue='diagnosis'
            )
            result = {**base, 'handled': 'attachment_precheck_dispatched',
                      'attachment_count': len(attachments)}
            _reply_intake(message_id, '已收到附件，将优先分析现有证据；暂不启动设备复现。')
        elif intake.intent == 'NEW_DIAGNOSIS' and intake.requires_device_access:
            from app.workers.device_provision_task import provision_from_feishu
            provision_from_feishu.apply_async(
                args=[text, chat_id, chat_type, source_context, True], queue='diagnosis'
            )
            result = {**base, 'handled': 'diagnosis_intake_dispatched', 'text': text[:80]}
            _reply_intake(message_id, accepted_text())
        elif intake.intent == 'CASE_FOLLOW_UP':
            if attachments:
                from app.workers.device_provision_task import ingest_feishu_attachments
                ingest_feishu_attachments.apply_async(
                    args=[text, chat_id, chat_type, source_context, attachments], queue='diagnosis'
                )
            if text:
                _dispatch_case_conversation(case_id=case.id, text=text, source_context=source_context)
            result = {
                **base, 'handled': 'case_conversation_dispatched',
                'follow_up_dispatched': bool(text),
                'attachment_follow_up_dispatched': bool(attachments),
            }
        else:
            result = {**base, 'handled': 'needs_clarification',
                      'missing_user_inputs': intake.missing_user_inputs or ['clarify_intent']}
            _reply_intake(message_id, '请说明要诊断的现象，并提供设备信息或上传抓包附件。')

        return _complete_message(db, handle, result, message_id, event_id)

    value = action_value(payload)
    action = str(value.get("action") or "").upper()
    is_card_action = event_type in CARD_ACTION_EVENT_TYPES

    if action == "AI2_ACCEPT_SUGGESTION":
        if not settings.feishu_identity_rbac_enabled:
            result = {"handled": "error", "reason": "AI2_SUGGESTION_RBAC_REQUIRED", "message": "AI2 建议采纳要求 Feishu Identity/RBAC 开启。"}
            return _card_action_response(result) if is_card_action else result
        case_id = str(value.get("case_id") or "")
        cycle_id = str(value.get("cycle_id") or "")
        from app.diagnosis.ai_suggest_bridge import AISuggestionBridge, AISuggestionBridgeError
        try:
            with db.begin_nested():
                execution = AISuggestionBridge().accept(
                    db,
                    case_id=case_id,
                    cycle_id=cycle_id,
                    actor=actor,
                    explicit_user_confirmation=True,
                )
            db.commit()
        except AISuggestionBridgeError as exc:
            result = {"handled": "error", "reason": str(exc), "message": f"无法采纳该 AI2 建议：{exc}"}
            return _card_action_response(result) if is_card_action else result
        except Exception as exc:
            result = {"handled": "error", "reason": type(exc).__name__, "message": "AI2 建议未进入确定性工作流，请检查 Case 状态后重试。"}
            return _card_action_response(result) if is_card_action else result

        if execution.enqueue_after_commit and execution.execution_ref_type == "reproduction_session" and execution.execution_ref_id:
            start_reproduction.apply_async(args=[execution.execution_ref_id], queue="reproduction-control")
        from app.workers.device_provision_task import sync_case_card
        sync_case_card.apply_async(args=[case_id, 'ai2_suggestion_accepted'], queue='diagnosis')
        result = {
            "handled": "ai2_suggestion_accepted",
            "case_id": case_id,
            "cycle_id": cycle_id,
            "kind": execution.kind,
            "registered_id": execution.registered_id,
            "execution_ref_type": execution.execution_ref_type,
            "execution_ref_id": execution.execution_ref_id,
            "message": execution.user_message,
            "idempotent_replay": execution.idempotent_replay,
            "ai_dispatch_authority": False,
        }
        return _card_action_response(result) if is_card_action else result

    if action == "STOP_REPRODUCTION":
        session_id = str(value.get("session_id") or "")
        row = db.get(ReproductionSession, session_id)
        if not row:
            result = {"handled": "error", "reason": "REPRODUCTION_NOT_FOUND"}
            return _card_action_response(result) if is_card_action else result
        cancel_reproduction.apply_async(args=[row.id], queue="reproduction-control-high")
        result = {"handled": "stop_reproduction", "session_id": session_id}
        return _card_action_response(result) if is_card_action else result
    if action == "EXTERNAL_ACTION_COMPLETED":
        experiment_id = str(value.get("experiment_id") or "")
        exp = db.get(DiagnosticExperiment, experiment_id)
        if not exp:
            result = {"handled": "error", "reason": "EXPERIMENT_NOT_FOUND"}
            return _card_action_response(result) if is_card_action else result
        run = db.scalar(
            select(ExperimentRun)
            .where(ExperimentRun.experiment_id == experiment_id)
            .order_by(ExperimentRun.run_no.desc())
            .limit(1)
        )
        if not run:
            result = {"handled": "error", "reason": "EXPERIMENT_RUN_NOT_FOUND"}
            return _card_action_response(result) if is_card_action else result
        DiagnosticExperimentOrchestrator().complete_external_action(db, run=run, actor=actor)
        db.commit()
        result = {"handled": "external_action_completed", "experiment_id": experiment_id}
        return _card_action_response(result) if is_card_action else result
    if action == "OPEN_CASE":
        result = {"handled": "open_case"}
        return _card_action_response(result) if is_card_action else result
    return {"handled": "unhandled", "event_type": event_type, "action": action}
