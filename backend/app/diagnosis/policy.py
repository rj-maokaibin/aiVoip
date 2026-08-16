from __future__ import annotations
from dataclasses import replace
from .types import PlanAction

class DiagnosisPlanPolicyError(RuntimeError): pass

POLICY={
    'RUN_MEDIA_ANALYSIS': {'risk':'L0','auto':True},
    'RUN_PACKET_ANALYSIS': {'risk':'L0','auto':True},
    'RUN_PCM_ANALYSIS': {'risk':'L0','auto':True},
    'RUN_FIELD_AUDIO_ANALYSIS': {'risk':'L0','auto':True},
    'RUN_IMAGE_METADATA_ANALYSIS': {'risk':'L0','auto':True},
    'RUN_FIELD_MEDIA_ALIGNMENT': {'risk':'L0','auto':True},
    'COLLECT_PROFILE': {'risk':'L1','auto':True},
    'REQUEST_USER_EVIDENCE': {'risk':'USER','auto':False},
    'REQUEST_MULTI_POINT_PCAP': {'risk':'USER','auto':False},
}

def enforce_plan_action(action:PlanAction) -> PlanAction:
    rule=POLICY.get(action.action_type)
    if not rule: raise DiagnosisPlanPolicyError(f'UNKNOWN_DIAGNOSIS_ACTION:{action.action_type}')
    params=dict(action.params or {})
    auto=bool(rule['auto'])
    if action.action_type=='COLLECT_PROFILE':
        # M4只允许自动跑已经审计的最小只读Profile；其他Profile必须后续通过审批扩展。
        if params.get('profile_id','voip_basic')!='voip_basic': auto=False
    if action.action_type in {'RUN_MEDIA_ANALYSIS','RUN_PCM_ANALYSIS'}:
        if params.get('profile_id','ruijie_aim_diag_v1')!='ruijie_aim_diag_v1': auto=False
    return replace(action,risk_level=rule['risk'],auto_execute=auto)
