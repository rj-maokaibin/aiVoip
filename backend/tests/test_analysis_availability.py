from app.rules.facts import build_rule_facts


def test_sip_unavailable_is_not_reported_as_zero_calls():
    snap={
        'case':{'summary':'电流音'},'devices':[],'evidences':[{'id':'e'}],
        'analyzers':{'media_intelligence':{'run_id':'r','result':{
            'packet':{'availability':{'sip':'UNAVAILABLE','rtp':'AVAILABLE'},'anomalies':[],'calls':[],'registrations':[],'rtp_streams':[{}]},
            'pcm':{'streams':[]},'correlations':[],'cross_layer_events':[]
        }}}
    }
    facts=build_rule_facts(snap)
    assert facts['sip']['available'] is False
    assert facts['sip']['call_count'] is None
    assert facts['sip']['registration_count'] is None
