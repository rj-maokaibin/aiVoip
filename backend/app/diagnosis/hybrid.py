from __future__ import annotations
from app.contracts.enums import HypothesisState
from app.diagnosis.gateway import ReasoningGatewayClient, ReasoningGatewayError
from app.diagnosis.reasoner import DeterministicDiagnosisReasoner
from app.diagnosis.types import DiagnosisDecision, EvidenceRef, HypothesisProposal, PlanAction
from app.diagnosis.policy import DiagnosisPlanPolicyError, enforce_plan_action

class HybridDiagnosisReasoner:
    version='0.1.0'
    def __init__(self,gateway:ReasoningGatewayClient|None=None):
        self.base=DeterministicDiagnosisReasoner(); self.gateway=gateway or ReasoningGatewayClient()

    def reason(self,snapshot:dict) -> DiagnosisDecision:
        baseline=self.base.reason(snapshot)
        if not self.gateway.enabled(): return baseline
        try: extra=self.gateway.enhance(snapshot,baseline.to_dict())
        except ReasoningGatewayError:
            baseline.summary={**baseline.summary,'llm_status':'DEGRADED','llm_note':'Reasoning Gateway不可用，已退化为确定性诊断。'}
            return baseline
        existing={h.code for h in baseline.hypotheses}
        for raw in (extra.get('hypotheses') or [])[:20]:
            try:
                code=str(raw['code'])[:128]
                if code in existing: continue
                baseline.hypotheses.append(HypothesisProposal(
                    code=code,title=str(raw['title'])[:512],fault_domain=str(raw.get('fault_domain','Other'))[:128],
                    confidence=min(0.75,max(0.0,float(raw.get('confidence',0.5)))),status=HypothesisState.OPEN.value,
                    rationale=str(raw.get('rationale','AI推断；尚无确定性证据确认。'))[:4000],confirmable=False,confirm_rule=None,
                    evidence=[EvidenceRef('AI_INFERENCE',self.gateway.model or 'reasoning-gateway','L5','SUPPORT',0.25,'大模型基于结构化证据的推断，不能替代直接证据。')]))
                existing.add(code)
            except Exception: continue
        rejected=0
        for raw in (extra.get('plan') or [])[:10]:
            try:
                proposed=PlanAction(str(raw['action_type']),str(raw.get('reason','AI建议补采'))[:1000],str(raw.get('risk_level','USER')),bool(raw.get('auto_execute',False)),dict(raw.get('params') or {}),int(raw.get('priority',80)))
                baseline.plan.append(enforce_plan_action(proposed))
            except (DiagnosisPlanPolicyError,Exception):
                rejected+=1; continue
        baseline.summary={**baseline.summary,'llm_status':'ENHANCED','llm_model':self.gateway.model,'llm_rejected_action_count':rejected}
        return baseline
