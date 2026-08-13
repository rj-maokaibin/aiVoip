from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.db.base import Base
from app.db.models import Case, DiagnosisRun, Job, Hypothesis, HypothesisEvidence
from app.diagnosis.reasoner import DeterministicDiagnosisReasoner
from app.services.diagnosis import persist_decision

def test_persist_hypothesis_and_direct_evidence():
    eng=create_engine('sqlite+pysqlite:///:memory:')
    Base.metadata.create_all(eng)
    result={'packet':{'anomalies':[{'type':'CODEC_NEGOTIATION_MISMATCH','severity':'HIGH','time':1,'evidence':{}}], 'calls':[], 'registrations':[], 'rtp_streams':[]},'correlations':[],'cross_layer_events':[]}
    snapshot={'case':{'summary':'通话异常'},'devices':[{}],'evidences':[{'id':'e','type':'PCAP','filename':'x.pcap'}], 'analyzers':{'media_intelligence':{'run_id':'ar','status':'SUCCESS','version':'1','summary':{},'result':result}},'fingerprint':'x'}
    decision=DeterministicDiagnosisReasoner().reason(snapshot)
    with Session(eng) as db:
        case=Case(case_no='C1',summary='x',status='ANALYZING'); db.add(case); db.flush()
        job=Job(case_id=case.id,type='AI_DIAGNOSIS',status='RUNNING'); db.add(job); db.flush()
        run=DiagnosisRun(case_id=case.id,job_id=job.id,status='ANALYZING'); db.add(run); db.flush()
        rows=persist_decision(db,run,decision); db.commit()
        h=next(x for x in rows if x.code=='CODEC_NEGOTIATION_MISMATCH')
        refs=list(db.scalars(select(HypothesisEvidence).where(HypothesisEvidence.hypothesis_id==h.id)))
        assert h.confirmable==1 and h.confidence>=9500
        assert refs and refs[0].evidence_level=='L1'
