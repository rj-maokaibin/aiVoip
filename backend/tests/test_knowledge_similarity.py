from app.knowledge.similarity import CaseSignature, CaseSimilarity, tokenize
from app.diagnosis.types import DiagnosisDecision, HypothesisProposal
from app.knowledge.service import enrich_decision_with_history


def sig(case_id,summary,codes=()):
    return CaseSignature(case_id,summary,set(codes),{'DTMF'} if codes else set(),set())


def test_chinese_summary_and_same_hypothesis_rank_high():
    a=sig('a','重启后第一次拨号丢第一位号码',['DTMF_FIRST_DIGIT_LOSS'])
    b=sig('b','设备重启后首次拨号会丢第一个号码，后续正常',['DTMF_FIRST_DIGIT_LOSS'])
    c=sig('c','SIP注册失败 403',['SIP_REGISTRATION_PATH_FAILURE'])
    ab,_=CaseSimilarity().score(a,b); ac,_=CaseSimilarity().score(a,c)
    assert ab>ac
    assert ab>0.35


def test_history_only_adds_l4_and_never_confirms():
    h=HypothesisProposal('H1','candidate','DTMF',0.70,'OPEN')
    d=DiagnosisDecision([h],[],'WAITING_USER',{})
    similar=[{'case_id':'old','case_no':'VOIP-OLD','summary':'same','status':'ROOT_CAUSE_CONFIRMED','score':0.9,'hypotheses':[{'code':'H1','title':'x','status':'CONFIRMED','confidence':0.99}]}]
    enrich_decision_with_history(d,similar)
    assert h.status=='OPEN'
    assert h.confidence<0.80
    assert h.evidence[-1].level=='L4'
