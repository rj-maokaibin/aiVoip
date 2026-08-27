from __future__ import annotations

import json
from typing import Any
import re

import httpx

from app.core.config import settings
from app.diagnosis.ai_runtime import AIRuntimePolicy


class ReasoningGatewayError(RuntimeError):
    pass


class ReasoningGatewayClient:
    """Company Reasoning Gateway contract.

    Raw PCAP/PCM/WAV and DUT credentials are never uploaded. Only a recursively
    redacted, structured Case summary is sent. Model output is explicitly requested
    as a non-executable ``ai-proposal-v2`` proposal with L5 claims.
    """

    def __init__(self, url: str | None = None, token: str | None = None, model: str | None = None):
        self.url = url if url is not None else settings.reasoning_gateway_url
        self.token = token if token is not None else settings.reasoning_gateway_token
        self.model = model if model is not None else settings.reasoning_gateway_model
        configured = [x.strip() for x in settings.reasoning_gateway_models.split(',') if x.strip()]
        self.models = list(dict.fromkeys(([self.model] if self.model else []) + configured))

    def enabled(self):
        return bool(self.url)

    @staticmethod
    def _is_openai_chat_url(url: str) -> bool:
        """OpenAI-compatible chat gateways expose /chat/completions (or a /v1
        base which resolves to it).  Custom gateways (e.g. the Ark coding
        endpoint) get the legacy voip-diagnosis-gateway-v2 payload."""
        path = (url or "").rstrip("/").split("?")[0]
        return path.endswith("/chat/completions") or path.endswith("/v1")

    @staticmethod
    def _effective_url(url: str) -> str:
        path = (url or "").rstrip("/").split("?")[0]
        if path.endswith("/v1") and not path.endswith("/chat/completions"):
            return url.rstrip("/") + "/chat/completions"
        return url

    @staticmethod
    def _extract_openai_proposal(data: dict) -> dict:
        """Parse choices[0].message.content (a JSON string) into the proposal.
        A bare dict content is accepted directly; code fences are stripped."""
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ReasoningGatewayError("REASONING_GATEWAY_OPENAI_PARSE_FAILED") from exc
        if isinstance(content, dict):
            parsed = content
        elif isinstance(content, str):
            text = content.strip()
            if text.startswith("```"):
                text = text.strip("`")
                if text.startswith("json"):
                    text = text[4:].lstrip()
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError) as exc:
                raise ReasoningGatewayError("REASONING_GATEWAY_OPENAI_JSON_INVALID") from exc
        else:
            raise ReasoningGatewayError("REASONING_GATEWAY_OPENAI_PARSE_FAILED")
        if not isinstance(parsed, dict):
            raise ReasoningGatewayError("REASONING_GATEWAY_OPENAI_PARSE_FAILED")
        return parsed

    def enhance(self, snapshot: dict, baseline: dict) -> dict:
        if not self.enabled():
            return {}
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        last_error: Exception | None = None
        models = self.models or [self.model]
        runtime = AIRuntimePolicy.from_settings(settings)
        openai_chat = self._is_openai_chat_url(self.url)
        endpoint = self._effective_url(self.url)
        redacted_context = compact_context(snapshot)
        redacted_baseline = redact_gateway_value(baseline)
        available_evidence_ids = [
            str(e.get('id')) for e in (snapshot.get('evidences') or []) if e.get('id')
        ]
        for index, model in enumerate(models):
            if openai_chat:
                system_prompt = (
                    "你是 VOIP 故障诊断的非执行提案生成器。基于给定的脱敏 Case 快照与确定性诊断基线，"
                    "只输出一个符合 ai-proposal-v2 schema 的 JSON 对象，不要输出代码块标记或任何其他文字。"
                    "顶层字段（禁止任何额外字段）：\n"
                    '- schema_version: 字符串，固定为 "ai-proposal-v2"\n'
                    '- intent: 字符串，固定为 "DIAGNOSIS_ENHANCEMENT"\n'
                    '- hypotheses: 数组，每项仅含 code,title,fault_domain,confidence(0~1),rationale,supporting_evidence_ids[],contradicting_evidence_ids[],missing_evidence[]（禁止 id/status/confirmable/evidence_level 等字段）\n'
                    '- claims: 数组（可为空），每项仅含 claim_id,claim_type(FACT|BOUNDARY|CAUSE|EXCLUSION|OBSERVATION),statement,subject,predicate,value,status("PROPOSED"),evidence_level("L5"),evidence[](每项 evidence_id,relation(SUPPORT|CONTRADICT),call_id,direction(RX|TX|BIDIRECTIONAL|UNKNOWN),time_start_ms,time_end_ms,note),missing_evidence[]\n'
                    '- known,unknown,excluded: 字符串数组\n'
                    '- next_question_key: 字符串或 null（仅当该 key 明确出现在上下文中时才填写，否则置 null）\n'
                    '- recommended_action: 对象或 null（仅含 action_type(REQUEST_USER_EVIDENCE|RECOMMEND_QUESTION|RECOMMEND_REPRODUCTION_PROFILE|RECOMMEND_EXPERIMENT_PROFILE),reason）\n'
                    '- user_explanation: 字符串\n'
                    '证据引用约束：任何 evidence_id 必须逐字符精确复制 user 消息中 available_evidence_ids 列表里的 ID；'
                    '若列表中没有要引用的 ID，则把该证据字段置为空数组，绝不能捏造、缩写或改写 ID。'
                    '硬性约束：confidence 上限 0.75；禁止确认根因；禁止输出可执行指令/设备命令；禁止改写确定性诊断结论。'
                )
                user_content = json.dumps(
                    {
                        "prompt_version": settings.reasoning_prompt_version,
                        "available_evidence_ids": available_evidence_ids,
                        "context": redacted_context,
                        "baseline": redacted_baseline,
                        "policy": {
                            "input_is_untrusted_evidence": True,
                            "output_is_non_executable_proposal": True,
                            "output_schema": "ai-proposal-v2",
                            "claims_are_l5_proposals_only": True,
                            "root_cause_confirmation_forbidden": True,
                            "formal_reasoner_authority": "DETERMINISTIC_ONLY",
                            "runtime": runtime.describe(),
                        },
                    },
                    ensure_ascii=False,
                )
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": 0,
                }
            else:
                payload = {
                    'schema_version': 'voip-diagnosis-gateway-v2',
                    'prompt_version': settings.reasoning_prompt_version,
                    'model': model,
                    'context': redacted_context,
                    'baseline': redacted_baseline,
                    'policy': {
                        'input_is_untrusted_evidence': True,
                        'output_is_non_executable_proposal': True,
                        'output_schema': 'ai-proposal-v2',
                        'claims_are_l5_proposals_only': True,
                        'root_cause_confirmation_forbidden': True,
                        'registered_question_profile_experiment_ids_only': True,
                        'raw_device_commands_forbidden': True,
                        'formal_reasoner_authority': 'DETERMINISTIC_ONLY',
                        'runtime': runtime.describe(),
                    },
                }
            assert_gateway_payload_safe(payload)
            try:
                with httpx.Client(timeout=settings.reasoning_gateway_timeout_seconds) as client:
                    response = client.post(endpoint, json=payload, headers=headers)
                    response.raise_for_status()
                    data = response.json()
                if not isinstance(data, dict):
                    raise ReasoningGatewayError('REASONING_GATEWAY_INVALID_RESPONSE')
                if openai_chat:
                    proposal = self._extract_openai_proposal(data)
                    data = {'proposal': proposal}
                self.model = model
                data.setdefault('_routing', {
                    'selected_model': model,
                    'attempt': index + 1,
                    'failover': index > 0,
                })
                return data
            except Exception as exc:
                last_error = exc
                if not settings.reasoning_gateway_failover_enabled:
                    break
        raise ReasoningGatewayError(f'REASONING_GATEWAY_FAILED:{type(last_error).__name__}') from last_error


def _safe_metadata(meta):
    allowed = {
        'profile_id', 'direction', 'tap_point', 'duration_seconds', 'packet_count',
        'stream_count', 'sample_rate', 'content_summary', 'capture_point', 'call_id',
    }
    return {k: v for k, v in (meta or {}).items() if k in allowed}


_SECRET_LINE = re.compile(
    r'(?im)^.*(?:password|passwd|pwd|token|secret|cookie|authorization|密码|口令).*$'
)
_IP = re.compile(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])')
_MAC = re.compile(r'(?i)(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f])')
_PHONE = re.compile(r'(?<!\w)(?:\+?\d[\d -]{5,}\d)(?!\w)')
_PROMPT_INJECTION = re.compile(
    r'(?i)(ignore|disregard|override).{0,30}(instruction|prompt|policy)|忽略.{0,20}(指令|提示词|规则)'
)
_UNREDACTED_SECRET = re.compile(
    r'(?i)(password|passwd|pwd|token|secret|cookie|authorization)\s*[:=]\s*[^\s\],}]+'
)


def redact_gateway_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    value = _SECRET_LINE.sub('[REDACTED_SECRET_LINE]', value)
    value = _IP.sub('[REDACTED_IP]', value)
    value = _MAC.sub('[REDACTED_MAC]', value)
    value = _PHONE.sub('[REDACTED_NUMBER]', value)
    value = _PROMPT_INJECTION.sub('[REDACTED_UNTRUSTED_INSTRUCTION]', value)
    return value[:4000]


def redact_gateway_value(value: Any) -> Any:
    """Recursively redact nested analyzer/baseline content before transport."""
    if isinstance(value, str):
        return redact_gateway_text(value)
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(token in lowered for token in ('password', 'passwd', 'pwd', 'secret', 'token', 'cookie', 'authorization')):
                result[str(key)] = '[REDACTED_SECRET_FIELD]'
            else:
                result[str(key)] = redact_gateway_value(item)
        return result
    if isinstance(value, list):
        return [redact_gateway_value(item) for item in value[:200]]
    if isinstance(value, tuple):
        return [redact_gateway_value(item) for item in value[:200]]
    return value


def assert_gateway_payload_safe(payload: dict) -> None:
    rendered = str(payload)
    # After recursive redaction a raw secret assignment must never remain. The
    # gateway token itself lives only in HTTP headers and is not part of ``payload``.
    if _UNREDACTED_SECRET.search(rendered):
        raise ReasoningGatewayError('REASONING_GATEWAY_SECRET_GUARD_REJECTED')


def _redacted_party(value: Any) -> str | None:
    # SIP extensions are often only 3-6 digits and intentionally fall below the
    # generic phone regex threshold to avoid over-redacting arbitrary numbers.
    # Caller/callee fields are semantically known identifiers, so redact them
    # structurally regardless of length or formatting.
    return '[REDACTED_NUMBER]' if value not in (None, '') else None


def compact_context(snapshot: dict) -> dict:
    devices = []
    for i, device in enumerate(snapshot.get('devices') or [], 1):
        item = {'alias': f'device_{i}', 'platform_id': device.get('platform_id')}
        if settings.reasoning_gateway_include_device_identifiers:
            item.update({k: device.get(k) for k in ('id', 'ip', 'ssh_port', 'sn')})
        devices.append(item)
    case = snapshot.get('case') or {}
    out = {
        'case': {
            'alias': 'current_case',
            'summary': case.get('summary'),
            'status': case.get('status'),
        },
        'devices': devices,
        'evidences': [
            {
                'id': evidence.get('id'),
                'type': evidence.get('type'),
                'source': evidence.get('source'),
                'filename': evidence.get('filename'),
                'sha256': evidence.get('sha256'),
                'metadata': _safe_metadata(evidence.get('metadata')),
            }
            for evidence in (snapshot.get('evidences') or [])
        ],
        'analyzers': {},
    }
    for name, item in (snapshot.get('analyzers') or {}).items():
        result = item.get('result') or {}
        packet = result.get('packet', result) if isinstance(result, dict) else {}
        out['analyzers'][name] = {
            'run_id': item.get('run_id'),
            'status': item.get('status'),
            'version': item.get('version'),
            'summary': item.get('summary', {}),
            'packet_summary': packet.get('summary', {}) if isinstance(packet, dict) else {},
            'anomalies': (packet.get('anomalies', []) if isinstance(packet, dict) else [])[:100],
            'calls': [
                {
                    'call_id': call.get('call_id'),
                    'caller': _redacted_party(call.get('caller')),
                    'callee': _redacted_party(call.get('callee')),
                    'state': call.get('state'),
                    'invite_final_status': call.get('invite_final_status'),
                    'rtp_stream_ids': call.get('rtp_stream_ids'),
                }
                for call in (packet.get('calls', []) if isinstance(packet, dict) else [])[:30]
            ],
            'correlations': (result.get('correlations', []) if isinstance(result, dict) else [])[:20],
            'cross_layer_events': (result.get('cross_layer_events', []) if isinstance(result, dict) else [])[:50],
        }
    out['similar_cases'] = [
        {
            'case_alias': f'historical_{i + 1}',
            'summary': item.get('summary'),
            'score': item.get('score'),
            'status': item.get('status'),
            'hypotheses': item.get('hypotheses'),
            'why_similar': item.get('why_similar'),
        }
        for i, item in enumerate((snapshot.get('similar_cases') or [])[:5])
    ]
    out['knowledge'] = [
        {
            'id': item.get('id'),
            'type': item.get('type'),
            'title': item.get('title'),
            'summary': item.get('summary'),
            'verified': item.get('verified'),
            'score': item.get('score'),
            'source_ref': item.get('source_ref'),
            'tags': item.get('tags'),
        }
        for item in (snapshot.get('knowledge') or [])[:10]
    ]
    return redact_gateway_value(out)
