from app.diagnosis.reasoner import DeterministicDiagnosisReasoner

R=DeterministicDiagnosisReasoner()

def snap(*,evidences=None,result=None,name='media_intelligence',summary='test'):
    analyzers={}
    if result is not None:
        analyzers[name]={'run_id':'run1','status':'SUCCESS','version':'x','result':result,'summary':result.get('summary',{})}
    return {'case':{'id':'c','summary':summary},'devices':[{'id':'d'}],'evidences':evidences or [],'analyzers':analyzers,'fingerprint':'x'}

def test_no_evidence_collects_voip_basic():
    d=R.reason(snap())
    assert d.conclusion_state=='NEED_MORE_EVIDENCE'
    assert d.plan[0].action_type=='COLLECT_PROFILE'
    assert d.plan[0].params['profile_id']=='voip_basic'
    assert d.plan[0].risk_level=='L1'

def test_pcap_without_media_auto_runs_media():
    d=R.reason(snap(evidences=[{'id':'e1','type':'PCAP','filename':'x.pcap'}]))
    a=next(x for x in d.plan if x.action_type=='RUN_MEDIA_ANALYSIS')
    assert a.auto_execute is True and a.risk_level=='L0'
    assert a.params['evidence_id']=='e1'

def test_high_delta_without_loss_is_not_packet_loss():
    result={'packet':{'anomalies':[{'type':'HIGH_DELTA','severity':'MEDIUM','time':1,'evidence':{}}], 'calls':[], 'registrations':[], 'rtp_streams':[{}]},'correlations':[],'cross_layer_events':[]}
    d=R.reason(snap(evidences=[{'id':'e','type':'PCAP','filename':'x.pcap'}],result=result))
    codes={h.code for h in d.hypotheses}
    assert 'RTP_ARRIVAL_JITTER' in codes
    assert 'RTP_PACKET_LOSS_PATH' not in codes

def test_burst_loss_creates_supported_hypothesis_and_multipoint_request():
    result={'packet':{'anomalies':[{'type':'BURST_LOSS','severity':'HIGH','time':1,'evidence':{}}], 'calls':[], 'registrations':[], 'rtp_streams':[{}], 'source':{'parser':'tshark'}},'correlations':[],'cross_layer_events':[]}
    d=R.reason(snap(evidences=[{'id':'e','type':'PCAP','filename':'x.pcap'}],result=result,summary='通话卡顿断音'))
    h=next(h for h in d.hypotheses if h.code=='RTP_PACKET_LOSS_PATH')
    assert h.status=='SUPPORTED' and h.confirmable is True
    assert any(a.action_type=='REQUEST_MULTI_POINT_PCAP' for a in d.plan)

def test_codec_mismatch_is_confirmable_direct_evidence():
    result={'packet':{'anomalies':[{'type':'CODEC_NEGOTIATION_MISMATCH','severity':'HIGH','time':1,'evidence':{}}], 'calls':[], 'registrations':[], 'rtp_streams':[]},'correlations':[],'cross_layer_events':[]}
    d=R.reason(snap(evidences=[{'id':'e','type':'PCAP','filename':'x.pcap'}],result=result))
    h=next(h for h in d.hypotheses if h.code=='CODEC_NEGOTIATION_MISMATCH')
    assert h.confidence>=.95 and h.confirmable
    assert h.evidence[0].level=='L1'

def test_dtmf_sip_match_marks_path_as_excluded_not_root_cause():
    result={'packet':{'anomalies':[], 'calls':[], 'registrations':[], 'rtp_streams':[]},'correlations':[], 'cross_layer_events':[{'type':'DTMF_SIP_DIAL_MATCH'}]}
    d=R.reason(snap(evidences=[{'id':'e','type':'PCAP','filename':'x.pcap'}],result=result))
    assert any('PCM输入→SIP拨号链路' in x for x in d.excluded)
