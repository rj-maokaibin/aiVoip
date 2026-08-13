from app.diagnosis.hybrid import HybridDiagnosisReasoner

class FakeGateway:
    model='fake'
    def enabled(self): return True
    def enhance(self,snapshot,baseline):
        return {'hypotheses':[{'code':'AI_GUESS','title':'AI候选','fault_domain':'Other','confidence':0.99,'rationale':'guess'}],
                'plan':[{'action_type':'RUN_SHELL','reason':'bad','risk_level':'L0','auto_execute':True,'params':{'command':'rm -rf /'}}]}

def test_llm_hypothesis_is_capped_and_l5_only_and_shell_rejected():
    r=HybridDiagnosisReasoner(FakeGateway())
    d=r.reason({'case':{'summary':'x'},'devices':[],'evidences':[{'id':'e','type':'LOG','filename':'x'}],'analyzers':{},'fingerprint':'x'})
    h=next(x for x in d.hypotheses if x.code=='AI_GUESS')
    assert h.confidence==0.75 and h.confirmable is False and h.evidence[0].level=='L5'
    assert all(x.action_type!='RUN_SHELL' for x in d.plan)
    assert d.summary['llm_rejected_action_count']==1
