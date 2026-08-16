"""Shared Feishu event dispatch used by both the HTTP callback and the WebSocket
long-connection listener.

Both transports normalize an incoming payload into the same header/event shape
and call dispatch_event, so provision / stop-reproduction / experiment actions
behave identically no matter how the event arrived (webhook vs long connection
-- the latter is used when the deployment has no public callback URL).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.models import (
    Case, CaseDevice, DiagnosticExperiment, ExperimentRun, FeishuCaseBinding,
    Hypothesis, ReproductionCall, ReproductionSession,
)
from app.experiments.orchestrator import DiagnosticExperimentOrchestrator
from app.integrations.feishu.intake import extract_message_content, route_intake
from app.integrations.feishu.feedback import accepted_text, enqueue_reply, status_text
from app.services.idempotency import begin_idempotent, complete_idempotent
from app.workers.reproduction_tasks import cancel_reproduction


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


# Card action-trigger event types (both the v2 schema `card.action.trigger` and
# the legacy `card.action.trigger_v1`). For these the callback must answer with
# a toast (and optionally an updated card) to give the user immediate feedback.
CARD_ACTION_EVENT_TYPES = {"card.action.trigger", "card.action.trigger_v1"}


def _correlated_case(db: Session, *, chat_id: str, case_ref: str | None,
                     message_id: str, root_message_id: str,
                     parent_message_id: str, device_refs: list[dict] | None = None,
                     symptoms: list[str] | None = None) -> tuple[Case | None, list[Case], str]:
    if case_ref:
        row = db.scalar(select(Case).where(Case.case_no == case_ref).limit(1))
        if row:
            return row, [], 'EXPLICIT_CASE_REF'
    keys = {x for x in (message_id, root_message_id, parent_message_id) if x}
    if chat_id and keys:
        row = db.scalar(
            select(Case).join(FeishuCaseBinding, FeishuCaseBinding.case_id == Case.id)
            .where(
                FeishuCaseBinding.receive_id == chat_id,
                or_(
                    FeishuCaseBinding.source_message_id.in_(keys),
                    FeishuCaseBinding.source_root_message_id.in_(keys),
                    FeishuCaseBinding.source_parent_message_id.in_(keys),
                ),
            ).order_by(Case.created_at.desc()).limit(1)
        )
        if row:
            return row, [], 'THREAD'

    # Cross-thread correlation is deliberately conservative: an exact device
    # identity AND a specific symptom must agree inside the same chat/time
    # window. Generic words such as "problem" never contribute to the score.
    specific = {str(x).lower() for x in (symptoms or [])
                if str(x).lower() not in {'故障', '异常', '问题'}}
    refs = device_refs or []
    if not chat_id or not refs or not specific:
        return None, [], 'NO_SAFE_MATCH'
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    cases = list(db.scalars(
        select(Case).join(FeishuCaseBinding, FeishuCaseBinding.case_id == Case.id)
        .where(
            FeishuCaseBinding.receive_id == chat_id,
            Case.created_at >= since,
            Case.status.not_in(['RESOLVED', 'CLOSED', 'FAILED']),
        ).order_by(Case.created_at.desc())
    ))
    scored: list[tuple[int, Case]] = []
    for candidate in cases:
        devices = list(db.scalars(select(CaseDevice).where(CaseDevice.case_id == candidate.id)))
        device_score = 0
        for ref in refs:
            for device in devices:
                info = device.device_info or {}
                if ref.get('sn') and str(ref['sn']).lower() == str(device.sn).lower():
                    device_score = max(device_score, 5)
                elif ref.get('ssh_ip') and str(ref['ssh_ip']) == str(device.ip):
                    device_score = max(device_score, 4)
                elif ref.get('mac') and str(ref['mac']).lower() == str(info.get('mac') or '').lower():
                    device_score = max(device_score, 4)
        symptom_score = 2 * sum(1 for token in specific if token in candidate.summary.lower())
        if device_score >= 4 and symptom_score >= 2:
            scored.append((device_score + symptom_score, candidate))
    if not scored:
        return None, [], 'NO_SAFE_MATCH'
    scored.sort(key=lambda item: (item[0], item[1].created_at), reverse=True)
    top_score = scored[0][0]
    top = [item[1] for item in scored if item[0] == top_score]
    if len(top) > 1:
        return None, top, 'AMBIGUOUS_FINGERPRINT'
    return top[0], [], 'DEVICE_SYMPTOM_TIME_WINDOW'


def _reply_intake(message_id: str, text: str) -> None:
    enqueue_reply(message_id, text)


def _card_action_response(result: dict) -> dict:
    """Map an action result to a Feishu card-action callback response.

    Adds a user-visible ``toast`` and, when the action mutates the case, an
    ``updated_card`` so the card is refreshed immediately (both formats are
    supported by card.action.trigger / card.action.trigger_v1).
    """
    handled = result.get("handled")
    if handled == "error":
        toast = {"type": "error", "content": "操作失败：请稍后重试"}
    elif handled == "stop_reproduction":
        toast = {"type": "info", "content": "已请求安全停止自动复现"}
    elif handled == "external_action_completed":
        toast = {"type": "success", "content": "已记录现场操作完成"}
    elif handled == "open_case":
        toast = {"type": "info", "content": "请在网页端查看 Case 详情"}
    else:
        toast = {"type": "info", "content": "已收到操作请求"}
    out = {"handled": handled, "toast": toast}
    if handled in {"stop_reproduction", "external_action_completed"}:
        out["updated_card"] = True
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


def dispatch_event(db: Session, *, payload: dict, actor: str = "feishu:callback") -> dict:
    """Handle a normalized Feishu event payload (im.message.receive_v1 text /
    card actions). Returns an out-dict with a human-readable summary.

    actor is used for STOP_REPRODUCTION / EXTERNAL_ACTION_COMPLETED actions; the
    caller should pass the extracted operator when available.
    """
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event_type = str(header.get("event_type") or payload.get("type") or "")

    if event_type == "im.message.receive_v1":
        event = payload.get("event") or {}
        msg = event.get("message") or {}
        chat_id = str(event.get("chat_id") or msg.get("chat_id") or "")
        # Both group and p2p events carry a conversation chat_id (oc_*). Keep
        # chat_type for policy/UX, but always bind delivery to that chat_id.
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
        source_context = {
            'tenant_key': str(header.get('tenant_key') or sender.get('tenant_key') or '') or None,
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
        # Feishu explicitly recommends message_id for duplicate delivery
        # suppression; event_id may change for a redelivery.
        idempotency_key = message_id or event_id or None
        semantic_payload = {
            'event_type': event_type, 'chat_id': chat_id, 'chat_type': chat_type,
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
        case, ambiguous_cases, correlation_reason = _correlated_case(
            db, chat_id=chat_id, case_ref=preliminary.case_ref,
            message_id=message_id, root_message_id=root_message_id,
            parent_message_id=parent_message_id,
            device_refs=preliminary.device_refs, symptoms=preliminary.symptoms,
        )
        intake = route_intake(text=text, attachments=attachments, has_thread_case=case is not None)
        base = {'chat_id': chat_id, 'chat_type': chat_type, 'event_id': event_id or None,
                'message_id': message_id or None, 'intent': intake.intent,
                'intake': intake.to_dict(), 'case_id': case.id if case else None,
                'correlation_reason': correlation_reason}

        if ambiguous_cases:
            case_nos = [row.case_no for row in ambiguous_cases[:3]]
            result = {**base, 'handled': 'needs_case_disambiguation',
                      'candidate_case_nos': case_nos,
                      'missing_user_inputs': ['explicit_case_reference']}
            _reply_intake(
                message_id,
                f'找到多个可能的 Case：{" / ".join(case_nos)}。请回复具体 Case 编号。',
            )
            complete_idempotent(db, handle, response=result, status_code=200,
                                resource_type='feishu_message',
                                resource_id=message_id or event_id or None)
            return result

        if case and correlation_reason == 'DEVICE_SYMPTOM_TIME_WINDOW':
            from app.services.audit import audit
            audit(db, case_id=case.id, actor=actor, event_type='FEISHU_CASE_CORRELATED',
                  target_type='case', target_id=case.id,
                  detail={'reason': correlation_reason, 'message_id': message_id,
                          'device_refs': preliminary.device_refs,
                          'symptoms': preliminary.symptoms})
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
                result = {**base, 'handled': 'status_query', 'case_no': case.case_no,
                          'case_status': case.status}
                _reply_intake(message_id, status_text(case))
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
                ingest_feishu_follow_up.apply_async(
                    args=[case.id, text, source_context], queue='diagnosis'
                )
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
                    complete_idempotent(db, handle, response=result, status_code=200,
                                        resource_type='feishu_message',
                                        resource_id=message_id or event_id or None)
                    return result
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
            from app.knowledge.answering import answer_verified_question
            answer = answer_verified_question(db, text)
            result = {**base, 'handled': 'general_question',
                      'answered': answer['answered'],
                      'citations': answer['citations']}
            _reply_intake(message_id, answer['text'])
            from app.services.audit import audit
            audit(db, case_id=case.id if case else None, actor=actor,
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
            from app.workers.device_provision_task import ingest_feishu_follow_up
            ingest_feishu_follow_up.apply_async(
                args=[case.id, text, source_context], queue='diagnosis'
            )
            result = {**base, 'handled': 'case_follow_up', 'follow_up_dispatched': True}
            _reply_intake(message_id, f'已将补充信息关联到 Case {case.case_no}。')
        else:
            result = {**base, 'handled': 'needs_clarification',
                      'missing_user_inputs': intake.missing_user_inputs or ['clarify_intent']}
            _reply_intake(message_id, '请说明要诊断的现象，并提供设备信息或上传抓包附件。')

        complete_idempotent(db, handle, response=result, status_code=200,
                            resource_type='feishu_message', resource_id=message_id or event_id or None)
        return result

    value = action_value(payload)
    action = str(value.get("action") or "").upper()
    is_card_action = event_type in CARD_ACTION_EVENT_TYPES

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
