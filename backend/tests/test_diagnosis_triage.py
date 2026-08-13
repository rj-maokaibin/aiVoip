from app.diagnosis.triage import triage_summary
from app.diagnosis.reasoner import DeterministicDiagnosisReasoner

def test_triage_audio_noise():
    assert 'AUDIO_NOISE' in triage_summary('通话过程中有明显电流音')

def test_jitter_does_not_claim_noise_root_cause():
    result={'packet':{'anomalies':[{'type':'HIGH_DELTA','severity':'MEDIUM','time':1,'evidence':{}}], 'calls':[], 'registrations':[], 'rtp_streams':[{}], 'source':{}},'correlations':[],'cross_layer_events':[]}
    s={'case':{'summary':'通话有电流音'},'devices':[{}],'evidences':[{'id':'e','type':'PCAP','filename':'x.pcap'}], 'analyzers':{'media_intelligence':{'run_id':'r','status':'SUCCESS','version':'1','summary':{},'result':result}},'fingerprint':'x'}
    d=DeterministicDiagnosisReasoner().reason(s)
    h=next(h for h in d.hypotheses if h.code=='RTP_ARRIVAL_JITTER')
    assert h.status=='OPEN' and h.confidence<.8
    assert d.conclusion_state=='WAITING_USER'
