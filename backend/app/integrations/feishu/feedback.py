from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from app.core.config import settings
from app.db.models import Case, FeishuCaseBinding
from app.services.idempotency import begin_idempotent, complete_idempotent


CASE_STATUS_LABELS = {
    'NEW': '已受理，等待开始诊断',
    'TRIAGING': '正在检查问题描述和诊断条件',
    'COLLECTING': '正在采集诊断证据',
    'ANALYZING': '正在分析已有证据',
    'NEED_MORE_EVIDENCE': '需要更多证据，正在准备下一步采集',
    'WAITING_USER': '等待您补充信息或完成现场操作',
    'DIAGNOSED': '已形成诊断结论',
    'ROOT_CAUSE_CONFIRMED': '已确认根因',
    'RESOLVING': '正在处理并验证修复效果',
    'RESOLVED': '问题已解决',
    'CLOSED': 'Case 已关闭',
    'FAILED': '诊断流程异常，等待重试或人工处理',
}


def human_case_status(status: str | None) -> str:
    value = str(status or 'NEW').upper()
    return CASE_STATUS_LABELS.get(value, f'处理中（{value}）')


def accepted_text() -> str:
    return '已受理。我会先检查已有证据，再决定是否需要采集或现场复现。'


def case_created_text(case_no: str) -> str:
    return f'已建立 Case {case_no}，正在检查设备信息和已有证据。'


def attachment_ready_text(case_no: str, count: int,
                          failed: list[dict] | None = None) -> str:
    base = f'Case {case_no} 已登记 {count} 个附件，正在优先分析附件，暂不启动设备复现。'
    if not failed:
        return base
    names = '、'.join(str(item.get('filename') or '未命名附件') for item in failed[:3])
    return f'{base} 以下附件未处理成功：{names}；请在原线程重新发送这些附件。'


def attachment_failed_text(failed: list[dict] | None = None) -> str:
    names = '、'.join(str(item.get('filename') or '未命名附件') for item in (failed or [])[:3])
    detail = f'（{names}）' if names else ''
    return f'附件{detail}下载或登记失败，未启动设备复现。请在原线程重新发送失败附件。'


def completed_text(case_no: str, headline: str | None) -> str:
    conclusion = (headline or '已形成诊断结果').strip()
    return f'Case {case_no} 诊断完成：{conclusion}。详情和证据请查看 Case 主卡。'


def failed_text(case_no: str | None = None) -> str:
    prefix = f'Case {case_no} ' if case_no else ''
    return f'{prefix}诊断流程遇到异常，已停止自动推进；不会执行未经确认的设备动作，请稍后重试或联系研发。'


def build_single_user_question(*, decision: dict | None = None,
                               summary: dict | None = None) -> str:
    """Return one field-facing question with explicit answer paths.

    Internal PCM/SLIC/aimd/SIP/RTP questions are intentionally never surfaced.
    ConversationState performs semantic de-duplication before this text is sent.
    Terminal no-progress/max-cycle states return a partial-conclusion notice rather
    than manufacturing yet another user question.
    """
    decision = decision or {}
    summary = summary or {}
    blocker = str(summary.get('blocking_reason') or '').upper()
    if blocker in {'MAX_CYCLES', 'NO_PROGRESS'}:
        known = list(summary.get('known') or [])[:3]
        unknown = list(summary.get('unknown') or [])[:3]
        parts = ['自动诊断已暂停，本轮不会继续重复追问同一信息。']
        if known:
            parts.append('当前已确认：' + '；'.join(str(x) for x in known) + '。')
        if unknown:
            parts.append('仍未确认：' + '；'.join(str(x) for x in unknown) + '。')
        parts.append('如果暂时没有新的直接证据，可以按现有证据先形成阶段结论；有新的抓包、录音或复现结果时也可以继续补充。')
        return ''.join(parts)
    for action in decision.get('plan') or []:
        if str(action.get('action_type') or '') != 'REQUEST_USER_EVIDENCE':
            continue
        need = {str(x).lower() for x in ((action.get('params') or {}).get('need') or [])}
        if need & {'device_or_pcap', 'device_url', 'device'}:
            return '请提供设备入口（URL，或 IP+SN）；如果暂时无法提供，也可以直接上传 PCAP/PCAPNG。'
        if any('timestamp' in x for x in need):
            return '请提供本次异常发生的大致时间；如果不知道，请回复“不知道”。'
        if any(x in {'pcap', 'pcap_or_pcapng', 'anomaly_timestamp_or_recording_or_new_capture'} or 'pcap' in x for x in need):
            return '请上传包含异常过程的 PCAP/PCAPNG；如果暂时无法抓取，请回复“暂时不能”。'
        if any('recording' in x or 'audio' in x for x in need):
            return '请上传异常时的现场录音；如果没有录音，请回复“没有”。'
    return '这个故障现在还能复现吗？请回复：可以 / 暂时不能 / 不确定。'


def enqueue_reply(message_id: str | None, text: str) -> bool:
    if not settings.feishu_live_enabled or not message_id:
        return False
    # The old CASE_FOLLOW_UP branch emitted this ack before the async worker knew
    # what the user's sentence meant. Conversation V1 lets the worker reply with
    # "what I understood + what changed + what happens next" instead.
    if settings.conversation_cycle_decoupled and text.startswith('已将补充信息关联到 Case '):
        return True
    from app.workers.device_provision_task import reply_feishu_text
    reply_feishu_text.apply_async(args=[message_id, text], queue='diagnosis')
    return True


def notify_case_once(db: Session, *, case_id: str, feedback_type: str,
                     token: str, text: str) -> dict[str, Any]:
    """Idempotently enqueue one active Feishu reply for a Case milestone.

    WAITING_USER questions are semantically de-duplicated using ConversationState
    rather than cycle number.  Non-question WAITING_USER notices such as
    MAX_CYCLES/NO_PROGRESS are delivered but never become an active question.
    """
    if not settings.feishu_live_enabled:
        return {'status': 'SKIPPED', 'reason': 'FEISHU_LIVE_DISABLED'}
    binding = db.scalar(select(FeishuCaseBinding).where(
        FeishuCaseBinding.case_id == case_id,
        FeishuCaseBinding.status == 'ACTIVE',
    ).limit(1))
    if not binding or not binding.source_message_id:
        return {'status': 'SKIPPED', 'reason': 'NO_SOURCE_MESSAGE'}

    semantic_token = token
    if feedback_type == 'WAITING_USER' and settings.conversation_cycle_decoupled:
        from app.conversation.state_service import ConversationStateService, need_from_question_text
        state_service = ConversationStateService()
        need = need_from_question_text(text)
        if need:
            question_state = state_service.mark_question_asked(
                db, case_id=case_id, text=text, need=need
            )
            if not question_state.get('should_ask'):
                return {
                    'status': 'SKIPPED',
                    'reason': question_state.get('reason') or 'QUESTION_SEMANTICALLY_SUPPRESSED',
                    'slot_key': question_state.get('slot_key'),
                    'slot_state': question_state.get('slot_state'),
                }
        semantic_token = state_service.semantic_feedback_key(db, case_id=case_id, text=text)

    key = f'{case_id}:{feedback_type}:{semantic_token}'
    handle = begin_idempotent(
        db, scope='FEISHU_CASE_FEEDBACK', key=key,
        payload={'case_id': case_id, 'feedback_type': feedback_type,
                 'message_id': binding.source_message_id, 'text': text},
    )
    if handle.replay is not None:
        return {**handle.replay, 'duplicate': True}
    queued = enqueue_reply(binding.source_message_id, text)
    response = {'status': 'QUEUED' if queued else 'SKIPPED',
                'feedback_type': feedback_type}
    complete_idempotent(db, handle, response=response, status_code=200,
                        resource_type='case', resource_id=case_id)
    return response


def status_text(case: Case) -> str:
    db = object_session(case)
    if db is not None and settings.conversation_cycle_decoupled:
        try:
            from app.conversation.response import GroundedConversationResponder
            return GroundedConversationResponder().render(
                db, case_id=case.id, intent='CASE_PROGRESS_QUERY'
            )
        except Exception:
            # Status queries must always have a deterministic fallback.
            pass
    return f'Case {case.case_no} 当前进度：{human_case_status(case.status)}。'