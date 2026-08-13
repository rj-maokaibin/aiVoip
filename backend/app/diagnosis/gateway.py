from __future__ import annotations
from typing import Any
import httpx
from app.core.config import settings

class ReasoningGatewayError(RuntimeError): pass

class ReasoningGatewayClient:
    """公司内部Reasoning Gateway契约。不会上传原始PCAP/PCM/WAV，只发送结构化诊断摘要。"""
    def __init__(self,url:str|None=None,token:str|None=None,model:str|None=None):
        self.url=url if url is not None else settings.reasoning_gateway_url
        self.token=token if token is not None else settings.reasoning_gateway_token
        self.model=model if model is not None else settings.reasoning_gateway_model

    def enabled(self): return bool(self.url)

    def enhance(self,snapshot:dict,baseline:dict) -> dict:
        if not self.enabled(): return {}
        headers={'Content-Type':'application/json'}
        if self.token: headers['Authorization']=f'Bearer {self.token}'
        payload={'schema_version':'voip-diagnosis-gateway-v1','prompt_version':settings.reasoning_prompt_version,'model':self.model,'context':compact_context(snapshot),'baseline':baseline}
        try:
            with httpx.Client(timeout=settings.reasoning_gateway_timeout_seconds) as client:
                r=client.post(self.url,json=payload,headers=headers); r.raise_for_status(); data=r.json()
        except Exception as exc:
            raise ReasoningGatewayError(f'REASONING_GATEWAY_FAILED:{type(exc).__name__}') from exc
        if not isinstance(data,dict): raise ReasoningGatewayError('REASONING_GATEWAY_INVALID_RESPONSE')
        return data


def _safe_metadata(meta):
    allowed={'profile_id','direction','tap_point','duration_seconds','packet_count','stream_count','sample_rate','content_summary','capture_point'}
    return {k:v for k,v in (meta or {}).items() if k in allowed}

def compact_context(snapshot:dict) -> dict:
    devices=[]
    for i,d in enumerate(snapshot.get('devices') or [],1):
        item={'alias':f'device_{i}','platform_id':d.get('platform_id')}
        if settings.reasoning_gateway_include_device_identifiers:
            item.update({k:d.get(k) for k in ('id','ip','ssh_port','sn')})
        devices.append(item)
    out={'case':{k:(snapshot.get('case') or {}).get(k) for k in ('id','case_no','summary','status')},'devices':devices,
         'evidences':[{'id':e.get('id'),'type':e.get('type'),'source':e.get('source'),'filename':e.get('filename'),'sha256':e.get('sha256'),'metadata':_safe_metadata(e.get('metadata'))} for e in (snapshot.get('evidences') or [])],
         'analyzers':{}}
    for name,item in (snapshot.get('analyzers') or {}).items():
        result=item.get('result') or {}; packet=result.get('packet',result) if isinstance(result,dict) else {}
        out['analyzers'][name]={
            'run_id':item.get('run_id'),'status':item.get('status'),'version':item.get('version'),'summary':item.get('summary',{}),
            'packet_summary':packet.get('summary',{}) if isinstance(packet,dict) else {},
            'anomalies':(packet.get('anomalies',[]) if isinstance(packet,dict) else [])[:100],
            'calls':[{k:c.get(k) for k in ('call_id','caller','callee','state','invite_final_status','rtp_stream_ids')} for c in (packet.get('calls',[]) if isinstance(packet,dict) else [])[:30]],
            'correlations':(result.get('correlations',[]) if isinstance(result,dict) else [])[:20],
            'cross_layer_events':(result.get('cross_layer_events',[]) if isinstance(result,dict) else [])[:50],
        }
    out['similar_cases']=[{k:x.get(k) for k in ('case_no','summary','score','status','hypotheses')} for x in (snapshot.get('similar_cases') or [])[:5]]
    out['knowledge']=[{k:x.get(k) for k in ('id','type','title','summary','verified','score','source_ref','tags')} for x in (snapshot.get('knowledge') or [])[:10]]
    return out
