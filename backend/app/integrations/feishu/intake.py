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
_SYMPTOM_WORDS = (
    '故障', '异常', '问题', '无声', '单通', '杂音', '噪音', '电流音', '回声', '断续', '卡顿',
    '丢包', '抖动', 'dtmf', '按键', '首位', '拨号', '呼叫失败', '注册失败', '打不通',
    '接不通', '没有声音', '听不到', '不响铃', '掉线', '音质', 'sip', 'rtp',
)
_DIAGNOSIS_WORDS = ('诊断', '排查', '分析', '帮忙看', '帮我看', '定位', '看看')


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


def route_intake(*, text: str, attachments: list[dict] | None = None,
                 has_thread_case: bool = False) -> IntakeResult:
    """Deterministic, fail-closed Feishu intent router.

    This router decides only workflow routing. It does not infer protocol facts,
    execute device actions, or use an LLM. Ambiguous input is returned with a
    missing-input list so the caller can ask one user-facing question.
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

    if any(word in lowered for word in ('停止复现', '停止诊断', '取消复现', 'stop reproduction', '停止抓取')):
        return IntakeResult('STOP_REPRODUCTION', 0.99, case_ref, devices, symptoms,
                            attachments, [], False, 'explicit_stop_phrase')
    if any(word in lowered for word in ('外部操作已完成', '现场操作已完成', '操作完成')):
        return IntakeResult('EXTERNAL_ACTION_COMPLETED', 0.96, case_ref, devices, symptoms,
                            attachments, [], False, 'explicit_external_action_completion')
    if any(word in lowered for word in ('修复完成', '已经修复', '已修复', 'fix applied', '修复已应用')):
        return IntakeResult('FIX_APPLIED', 0.96, case_ref, devices, symptoms,
                            attachments, [], False, 'explicit_fix_completion')
    if any(word in lowered for word in ('进度', '状态', '到哪了', '结果了吗', '诊断结果', 'status')):
        missing = [] if (case_ref or has_thread_case) else ['case_reference_or_reply_in_case_thread']
        return IntakeResult('STATUS_QUERY', 0.94 if not missing else 0.65, case_ref, devices,
                            symptoms, attachments, missing, False, 'status_phrase')

    question_language = text.endswith(('?', '？')) or any(
        word in lowered for word in ('怎么', '什么', '为什么', '如何')
    )
    explicit_diagnosis = any(word in lowered for word in _DIAGNOSIS_WORDS)
    if question_language and not attachments and not device.is_open_intent() and not explicit_diagnosis:
        return IntakeResult('GENERAL_QUESTION', 0.88, case_ref, devices, symptoms,
                            attachments, [], False, 'question_language')

    diagnosis_language = bool(symptoms or explicit_diagnosis)
    if attachments or diagnosis_language or device.is_open_intent():
        missing: list[str] = []
        if not attachments and not symptoms and not any(word in lowered for word in _DIAGNOSIS_WORDS):
            missing.append('symptom_description')
        if not attachments and not device.has_minimal():
            missing.append('device_url_or_ip_and_sn_or_attachment')
        confidence = 0.95 if not missing else 0.68
        return IntakeResult(
            'NEW_DIAGNOSIS', confidence, case_ref, devices, symptoms, attachments,
            missing, bool(not attachments and device.has_minimal() and not missing),
            'attachment_or_diagnosis_signal',
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
