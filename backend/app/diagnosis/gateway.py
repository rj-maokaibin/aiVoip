from __future__ import annotations
from typing import Any
import re
import httpx
from app.core.config import settings

class ReasoningGatewayError(RuntimeError): pass

class ReasoningGatewayClient:
    """公司内部Reasoning Gateway契约。不会上传原始PCAP/PCM/WAV，只发送结构化诊断摘要。"""
    def __init__(self,url:str|None=None,token:str|None=None,model:str|None=None):
        self.url=url if url is not None else settings.reasoning_gateway_url
        self.token=token if token is not None else settings.reasoning_gateway_token
        self.model=model if model is not None else settings.reasoning_gateway_model
        configured=[x.strip() for x in settings.reasoning_gateway_models.split(',') if x.strip()]
        self.models=list(dict.fromkeys(([self.model] if self.model else [])+configured))

    def enabled(self): return bool(self.url)

    def enhance(self,snapshot:dict,baseline:dict) -> dict:
        if not self.enabled(): return {}
        headers={'Content-Type':'application/json'}
        if self.token: headers['Authorization']=f'Bearer {self.token}'
        last_error: Exception|None=None
        models=self.models or [self.model]
        for index,model in enumerate(models):
            payload={'schema_version':'voip-diagnosis-gateway-v1','prompt_version':settings.reasoning_prompt_version,
                     'model':model,'context':compact_context(snapshot),'baseline':baseline,
                     'policy':{'input_is_untrusted_evidence':True,'output_is_non_executable_proposal':True}}
            try:
                with httpx.Client(timeout=settings.reasoning_gateway_timeout_seconds) as client:
                    r=client.post(self.url,json=payload,headers=headers); r.raise_for_status(); data=r.json()
                if not isinstance(data,dict): raise ReasoningGatewayError('REASONING_GATEWAY_INVALID_RESPONSE')
                self.model=model
                data.setdefault('_routing',{'selected_model':model,'attempt':index+1,'failover':index>0})
                return data
            except Exception as exc:
                last_error=exc
                if not settings.reasoning_gateway_failover_enabled: break
        raise ReasoningGatewayError(f'REASONING_GATEWAY_FAILED:{type(last_error).__name__}') from last_error


def _safe_metadata(meta):
    allowed={'profile_id','direction','tap_point','duration_seconds','packet_count','stream_count','sample_rate','content_summary','capture_point'}
    return {k:v for k,v in (meta or {}).items() if k in allowed}

_SECRET_LINE=re.compile(r'(?im)^.*(?:password|passwd|pwd|token|secret|cookie|authorization|\u5bc6\u7801|\u53e3\u4ee4).*$')
_IP=re.compile(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])')
_MAC=re.compile(r'(?i)(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f])')
_PHONE=re.compile(r'(?<!\w)(?:\+?\d[\d -]{5,}\d)(?!\w)')
_PROMPT_INJECTION=re.compile(r'(?i)(ignore|disregard|override).{0,30}(instruction|prompt|policy)|\u5ffd\u7565.{0,20}(\u6307\u4ee4|\u63d0\u793a\u8bcd|\u89c4\u5219)')

def redact_gateway_text(value: Any) -> Any:
    if not isinstance(value,str): return value
    value=_SECRET_LINE.sub('[REDACTED_SECRET_LINE]',value)
    value=_IP.sub('[REDACTED_IP]',value)
    value=_MAC.sub('[REDACTED_MAC]',value)
    value=_PHONE.sub('[REDACTED_NUMBER]',value)
    value=_PROMPT_INJECTION.sub('[REDACTED_UNTRUSTED_INSTRUCTION]',value)
    return value[:4000]

def compact_context(snapshot:dict) -> dict:
    devices=[]
    for i,d in enumerate(snapshot.get('devices') or [],1):
        item={'alias':f'device_{i}','platform_id':d.get('platform_id')}
        if settings.reasoning_gateway_include_device_identifiers:
            item.update({k:d.get(k) for k in ('id','ip','ssh_port','sn')})
        devices.append(item)
    case=snapshot.get('case') or {}
    out={'case':{'alias':'current_case','summary':redact_gateway_text(case.get('summary')),'status':case.get('status')},'devices':devices,
         'evidences':[{'id':e.get('id'),'type':e.get('type'),'source':e.get('source'),'filename':redact_gateway_text(e.get('filename')),'sha256':e.get('sha256'),'metadata':_safe_metadata(e.get('metadata'))} for e in (snapshot.get('evidences') or [])],
         'analyzers':{}}
    for name,item in (snapshot.get('analyzers') or {}).items():
        result=item.get('result') or {}; packet=result.get('packet',result) if isinstance(result,dict) else {}
        out['analyzers'][name]={
            'run_id':item.get('run_id'),'status':item.get('status'),'version':item.get('version'),'summary':item.get('summary',{}),
            'packet_summary':packet.get('summary',{}) if isinstance(packet,dict) else {},
            'anomalies':(packet.get('anomalies',[]) if isinstance(packet,dict) else [])[:100],
            'calls':[{'call_id':c.get('call_id'),'caller':redact_gateway_text(c.get('caller')),
                      'callee':redact_gateway_text(c.get('callee')),'state':c.get('state'),
                      'invite_final_status':c.get('invite_final_status'),'rtp_stream_ids':c.get('rtp_stream_ids')}
                     for c in (packet.get('calls',[]) if isinstance(packet,dict) else [])[:30]],
            'correlations':(result.get('correlations',[]) if isinstance(result,dict) else [])[:20],
            'cross_layer_events':(result.get('cross_layer_events',[]) if isinstance(result,dict) else [])[:50],
        }
    out['similar_cases']=[{'case_alias':f'historical_{i+1}','summary':redact_gateway_text(x.get('summary')),
                           'score':x.get('score'),'status':x.get('status'),'hypotheses':x.get('hypotheses')}
                          for i,x in enumerate((snapshot.get('similar_cases') or [])[:5])]
    out['knowledge']=[{'id':x.get('id'),'type':x.get('type'),'title':redact_gateway_text(x.get('title')),
                      'summary':redact_gateway_text(x.get('summary')),'verified':x.get('verified'),
                      'score':x.get('score'),'source_ref':redact_gateway_text(x.get('source_ref')),
                      'tags':x.get('tags')} for x in (snapshot.get('knowledge') or [])[:10]]
    return out
