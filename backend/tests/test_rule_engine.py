import pytest
from app.rules.compiler import RuleCompileError, compile_rule
from app.rules.engine import RuleEngine


def snapshot(summary='通话卡顿', anomalies=None):
    return {
        'case':{'summary':summary},'devices':[],'evidences':[{'id':'e1'}],
        'analyzers':{'media_intelligence':{'run_id':'run1','result':{'packet':{'anomalies':anomalies or [],'calls':[],'registrations':[],'rtp_streams':[]},'pcm':{'streams':[]},'correlations':[],'cross_layer_events':[]}}}
    }


def test_rule_burst_loss_matches_and_is_traceable():
    rule=compile_rule({'key':'R1','version':'1','dsl_version':2,'name':'burst','fault_domain':'RTP/Network','when':{'and':[{'path':'symptoms.AUDIO_STUTTER','op':'eq','value':True},{'path':'anomaly_counts.BURST_LOSS','op':'gte','value':1}]},'then':[{'action':'hypothesis','payload':{'code':'RTP_PACKET_LOSS_PATH','title':'RTP burst','confidence':0.95,'status':'SUPPORTED','evidence_level':'L1'}}]})
    effects,matches,facts=RuleEngine().evaluate(snapshot(anomalies=[{'type':'BURST_LOSS'}]),[rule])
    assert matches[0].matched is True
    assert effects['hypotheses'][0].code=='RTP_PACKET_LOSS_PATH'
    assert effects['hypotheses'][0].evidence[0].ref_type=='ANALYZER_RUN'
    assert effects['hypotheses'][0].evidence[0].level=='L1'
    assert facts['anomaly_counts']['BURST_LOSS']==1


def test_rule_does_not_match_jitter_as_loss():
    rule=compile_rule({'key':'R1','version':'1','dsl_version':2,'name':'burst','when':{'and':[{'path':'anomaly_counts.BURST_LOSS','op':'gte','value':1}]},'then':[{'action':'known','payload':{'text':'loss'}}]})
    effects,matches,_=RuleEngine().evaluate(snapshot(anomalies=[{'type':'HIGH_DELTA'}]),[rule])
    assert matches[0].matched is False
    assert effects['known']==[]


def test_rule_rejects_arbitrary_expression_and_action():
    with pytest.raises(RuleCompileError):
        compile_rule({'key':'BAD','version':'1','dsl_version':2,'when':{'and':[{'path':'__import__.os','op':'eq','value':1}]},'then':[{'action':'known','payload':{'text':'x'}}]})
    with pytest.raises(RuleCompileError):
        compile_rule({'key':'BAD','version':'1','dsl_version':2,'when':{'and':[{'path':'rtp.stream_count','op':'gte','value':1}]},'then':[{'action':'run_shell','payload':{'command':'rm -rf /'}}]})


def test_rule_periodic_local_capture_matches_audio_noise():
    rule=compile_rule({'key':'P','version':'1','dsl_version':2,'name':'periodic','when':{'and':[{'path':'symptoms.AUDIO_NOISE','op':'eq','value':True},{'path':'pcm.local_capture_periodic_interference_count','op':'gte','value':1}]},'then':[{'action':'hypothesis','payload':{'code':'LOCAL_CAPTURE_PERIODIC_INTERFERENCE','title':'periodic','confidence':0.96,'status':'SUPPORTED','evidence_level':'L1'}}]})
    s=snapshot(summary='持续电流音')
    s['analyzers']['media_intelligence']['result']['cross_layer_events']=[{'type':'LOCAL_CAPTURE_PERIODIC_INTERFERENCE'}]
    effects,matches,facts=RuleEngine().evaluate(s,[rule])
    assert matches[0].matched is True
    assert facts['pcm']['local_capture_periodic_interference_count']==1
    assert effects['hypotheses'][0].code=='LOCAL_CAPTURE_PERIODIC_INTERFERENCE'


def test_legacy_v1_rule_is_readable_but_v2_is_the_write_contract():
    rule=compile_rule({'key':'LEGACY','version':'1','dsl_version':1,'when':{'all':[{'path':'symptoms.AUDIO_STUTTER','op':'truthy'}]},'then':[{'action':'known','payload':{'text':'legacy'}}]})
    effects,matches,_=RuleEngine().evaluate(snapshot(),[rule])
    assert matches[0].matched is True
    assert effects['known']==['legacy']
