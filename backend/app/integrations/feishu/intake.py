from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from app.integrations.feishu.device_request import DeviceAccessRequest, parse_device_request


INTENTS = {
    'NEW_DIAGNOSIS', 'CASE_FOLLOW_UP', 'STATUS_QUERY', 'STOP_REPRODUCTION',
    'EXTERNAL_ACTION_COMPLETED', 'FIX_APPLIED', 'GENERAL_QUESTION', 'UNSUPPORTED',
}

_CASE_RE = re.compile(r'\b(?:VOIP-\d{8}-[A-Z0-9]{6}|CASE[-_:#： ]?[A-Z0-9-]+)\b', re.I)
_STATUS_RE = re.compile(
    r'(?:进度|状态|到哪(?:了)?|结果(?:了吗|出来了吗|有了吗)?|诊断结果|status|'
    r'什么时候.*(?:结束|完成)|还要多久|多久.*(?:结束|完成)|'
    r'(?:分析|诊断).*(?:结束|完成)(?:了吗|了没|没有)?|'
    r'可以结束(?:分析|诊断)?吗|能结束(?:分析|诊断)?吗|'
    r'还需要我做什么|需要我做什么|我还要做什么|下一步(?:做什么|怎么办)?|'
    r'还缺什么|还需要什么|需要补充什么)',
    re.I,
)
_SYMPTOM_WORDS = (
    '故障', '异常', '问题', '无声', '单通', '杂音', '噪音', '电流音', '回声', '断续', '卡顿',
    '丢包', '抖动', 'dtmf', '按键', '首位', '拨号', '呼叫失败', '注册失败', '打不通',
    '接不通', '没有声音', '听不到', '不响铃', '掉线', '音质', 'sip', 'rtp', '不生效', '没反应',
)
_DIAGNOSIS_WORDS = ('诊断', '排查', '分析', '帮忙看', '帮我看', '定位', '看看')
_INCIDENT_WORDS = (
    '客户', '现场', '这台', '这个设备', '我这边', '我们这边', '当前', '现在', '一直', '实际',
    '出现', '发生', '复现', '不生效', '没反应', '失败', '异常', '故障',
)
_CONTROL_PUNCT = re.compile(r'[\s，,。.!！；;：:、]+')
_CONTINUE_CONTROL_RE = re.compile(
    r'^(?:继续|继续分析|继续诊断|继续吧|往下分析|好的继续|好继续|恢复分析|恢复诊断)[。.!！ ]*$',
    re.I,
)
_FINISH_EXACT_RE = re.compile(
    r'^(?:结束吧|结束分析|结束诊断|按现有证据出结论|按现有结果出结论|给阶段结论)[。.!！ ]*$',
    re.I,
)


@dataclass(frozen=True)
class IntakeResult:
    intent: str
    confidence: float
    case_ref: str | None = None
    device_refs: list[dict[str, Any]] = field(default_factory=list)
    symptoms: list[str] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    missing_user_inputs: list[str] = field(default_factory=list)
    requires_device_access: bool = False
    reason: str = ''

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _device_ref(request: DeviceAccessRequest) -> list[dict[str, Any]]:
    if not any((request.web_url, request.ssh_ip, request.sn, request.mac, request.product)):
        return []
    return [{
        'web_url': request.web_url, 'ssh_ip': request.ssh_ip,
        'ssh_port': request.ssh_port, 'sn': request.sn,
        'mac': request.mac, 'product': request.product,
    }]


def _symptoms(text: str) -> list[str]:
    lowered = text.lower()
    return [word for word in _SYMPTOM_WORDS if word.lower() in lowered]


def _compact_control_text(text: str) -> str:
    return _CONTROL_PUNCT.sub('', (text or '').strip().lower())


def is_finish_control_text(text: str) -> bool:
    """Recognize an explicit request to end this analysis with current evidence.

    This mirrors the Conversation control contract closely enough for the Feishu
    ingress layer to route the turn into the already-active Case. The Conversation
    interpreter remains authoritative for mutating the finish-control state.
    """
    raw = (text or '').strip()
    if not raw:
        return False
    compact = _compact_control_text(raw)
    if not compact:
        return False
    if raw.endswith(('?', '？')) or compact.startswith(('为什么', '为何', '怎么')):
        return False
    if any(token in compact for token in ('不要结束', '别结束', '先不要结束', '暂时不要结束', '不用结束')):
        return False

    if _FINISH_EXACT_RE.fullmatch(raw):
        return True
    if any(token in compact for token in (
        '结束本轮分析',
        '停止本轮分析',
        '停止分析',
        '停止诊断',
        '本轮先结束',
        '先结束本轮分析',
    )):
        return True

    has_conclusion = '阶段结论' in compact or compact.endswith('出结论') or '形成结论' in compact
    grounded_scope = any(token in compact for token in (
        '现有证据',
        '当前证据',
        '现有结果',
        '当前结果',
        '当前pcap',
        '当前这个pcap',
        '已有证据',
    ))
    stop_waiting = any(token in compact for token in ('不要再等', '不再等', '不用再等', '别再等'))
    conclusion_verb = any(token in compact for token in (
        '给阶段结论',
        '出阶段结论',
        '形成阶段结论',
        '直接给阶段结论',
        '先给阶段结论',
        '给出阶段结论',
        '出结论',
        '形成结论',
    ))
    return bool(has_conclusion and (grounded_scope or stop_waiting or conclusion_verb))


def is_continue_control_text(text: str) -> bool:
    return bool(_CONTINUE_CONTROL_RE.fullmatch((text or '').strip()))


def route_intake(*, text: str, attachments: list[dict] | None = None,
                 has_thread_case: bool = False) -> IntakeResult:
    """Deterministic, fail-closed Feishu workflow router.

    The deterministic layer preserves the frozen AI1 routing contract. It does
    not infer protocol facts or execute actions. Conversation semantics such as
    ``KNOWLEDGE_IN_CASE`` and ``HYBRID`` are resolved by the Conversation layer
    after a Case is correlated. This keeps legacy router behavior stable while
    allowing the richer Conversation Platform to own context-aware interpretation.

    Case-level conversation controls are special: the ingress router must route
    them into the correlated Case before generic diagnosis detection sees words
    such as ``分析`` or ``诊断``. The Conversation interpreter is still the sole
    authority that interprets FINISH/CONTINUE and changes Conversation state.
    """
    text = (text or '').strip()
    lowered = text.lower()
    attachments = list(attachments or [])
    try:
        device = parse_device_request(text) if text else DeviceAccessRequest(raw='')
    except Exception:
        device = DeviceAccessRequest(raw=text)
    devices = _device_ref(device)
    symptoms = _symptoms(text)
    case_match = _CASE_RE.search(text)
    case_ref = case_match.group(0).upper() if case_match else None

    # Reproduction controls remain operational controls. Do not let the broader
    # diagnosis-finish semantics steal explicit reproduction/capture commands.
    if any(word in lowered for word in ('停止复现', '取消复现', 'stop reproduction', '停止抓取')):
        return IntakeResult('STOP_REPRODUCTION', 0.99, case_ref, devices, symptoms,
                            attachments, [], False, 'explicit_stop_phrase')

    # Finish/continue is a Case conversation control, not a new incident. The
    # preliminary pass may not know the active Case yet, so use STATUS_QUERY only
    # as a fail-closed correlation carrier; the second pass with a resolved Case
    # becomes CASE_FOLLOW_UP and reaches the Conversation interpreter.
    if is_finish_control_text(text) or is_continue_control_text(text):
        if has_thread_case or case_ref:
            return IntakeResult('CASE_FOLLOW_UP', 0.99, case_ref, devices, symptoms,
                                attachments, [], False, 'explicit_conversation_control')
        return IntakeResult(
            'STATUS_QUERY', 0.72, case_ref, devices, symptoms, attachments,
            ['case_reference_or_reply_in_case_thread'], False,
            'conversation_control_requires_case',
        )

    if any(word in lowered for word in ('外部操作已完成', '现场操作已完成', '操作完成')):
        return IntakeResult('EXTERNAL_ACTION_COMPLETED', 0.96, case_ref, devices, symptoms,
                            attachments, [], False, 'explicit_external_action_completion')
    if any(word in lowered for word in ('修复完成', '已经修复', '已修复', 'fix applied', '修复已应用')):
        return IntakeResult('FIX_APPLIED', 0.96, case_ref, devices, symptoms,
                            attachments, [], False, 'explicit_fix_completion')
    if _STATUS_RE.search(text):
        missing = [] if (case_ref or has_thread_case) else ['case_reference_or_reply_in_case_thread']
        return IntakeResult('STATUS_QUERY', 0.97 if not missing else 0.68, case_ref, devices,
                            symptoms, attachments, missing, False, 'status_or_completion_phrase')

    question_language = text.endswith(('?', '？')) or any(
        word in lowered for word in ('怎么', '什么', '为什么', '如何', '会不会', '是不是')
    )
    explicit_diagnosis = any(word in lowered for word in _DIAGNOSIS_WORDS)
    incident_language = bool(
        has_thread_case and symptoms and any(word in lowered for word in _INCIDENT_WORDS)
    )
    if (question_language and not attachments and not device.is_open_intent()
            and not explicit_diagnosis and not incident_language):
        return IntakeResult('GENERAL_QUESTION', 0.88, case_ref, devices, symptoms,
                            attachments, [], False, 'question_language')

    diagnosis_language = bool(symptoms or explicit_diagnosis)
    if attachments or diagnosis_language or device.is_open_intent():
        missing: list[str] = []
        if not attachments and not symptoms and not any(word in lowered for word in _DIAGNOSIS_WORDS):
            missing.append('symptom_description')
        if not attachments and not device.has_minimal() and not has_thread_case:
            missing.append('device_url_or_ip_and_sn_or_attachment')
        confidence = 0.95 if not missing else 0.68
        return IntakeResult(
            'NEW_DIAGNOSIS', confidence, case_ref, devices, symptoms, attachments,
            missing, bool(not attachments and device.has_minimal() and not missing),
            'mixed_incident_question' if incident_language else 'attachment_or_diagnosis_signal',
        )

    if has_thread_case and text:
        return IntakeResult('CASE_FOLLOW_UP', 0.82, case_ref, devices, symptoms,
                            attachments, [], False, 'active_thread_context')
    return IntakeResult('UNSUPPORTED', 0.45, case_ref, devices, symptoms,
                        attachments, ['clarify_intent'], False, 'no_safe_route')


def extract_message_content(message: dict) -> tuple[str, list[dict[str, Any]]]:
    """Normalize Feishu text/file/image/audio/media/post message content."""
    msg_type = str(message.get('message_type') or message.get('msg_type') or 'text').lower()
    content = message.get('content') or {}
    if isinstance(content, str):
        import json
        try:
            content = json.loads(content)
        except Exception:
            content = {}
    if not isinstance(content, dict):
        content = {}
    attachments: list[dict[str, Any]] = []
    text_parts: list[str] = []

    if msg_type == 'text':
        text_parts.append(str(content.get('text') or ''))
    elif msg_type in {'file', 'audio', 'media', 'image'}:
        key = content.get('image_key') if msg_type == 'image' else content.get('file_key')
        if key:
            default_name = f'{msg_type}-{str(key)[-12:]}'
            attachments.append({
                'file_key': str(key),
                'filename': str(content.get('file_name') or default_name),
                'message_type': msg_type,
                'resource_type': 'image' if msg_type == 'image' else 'file',
            })
    elif msg_type == 'post':
        def walk(value):
            if isinstance(value, dict):
                if value.get('tag') == 'text' and value.get('text'):
                    text_parts.append(str(value['text']))
                if value.get('tag') == 'img' and value.get('image_key'):
                    key = str(value['image_key'])
                    attachments.append({'file_key': key, 'filename': f'image-{key[-12:]}.png',
                                        'message_type': 'image', 'resource_type': 'image'})
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(content)
    return '\n'.join(x for x in text_parts if x).strip(), attachments
