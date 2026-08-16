from __future__ import annotations

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

    def enhance(self, snapshot: dict, baseline: dict) -> dict:
        if not self.enabled():
            return {}
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        last_error: Exception | None = None
        models = self.models or [self.model]
        runtime = AIRuntimePolicy.from_settings(settings)
        for index, model in enumerate(models):
            payload = {
                'schema_version': 'voip-diagnosis-gateway-v2',
                'prompt_version': settings.reasoning_prompt_version,
                'model': model,
                'context': compact_context(snapshot),
                'baseline': redact_gateway_value(baseline),
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
                    response = client.post(self.url, json=payload, headers=headers)
                    response.raise_for_status()
                    data = response.json()
                if not isinstance(data, dict):
                    raise ReasoningGatewayError('REASONING_GATEWAY_INVALID_RESPONSE')
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
    r'(?i)(password|passwd|pwd|token|secret|cookie|authorization)\s*[:=]\s*[^\s\],}]+ '
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
    if _UNREDACTED_SECRET.search(rendered + ' '):
        raise ReasoningGatewayError('REASONING_GATEWAY_SECRET_GUARD_REJECTED')


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
                    'caller': call.get('caller'),
                    'callee': call.get('callee'),
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
