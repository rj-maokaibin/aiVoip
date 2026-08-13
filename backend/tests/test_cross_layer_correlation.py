from app.analyzers.correlation import correlate_pcm_dtmf_with_sip

def test_pcm_dtmf_matches_sip_target():
    packet={"calls":[{"call_id":"c","callee":"sip:8803@example","start_time":110.0}]}
    pcm={"streams":[{"tap":{"name":"pcm_rx","direction":"RX"},"sessions":[{"session_index":2,"start_time":100.0,"dtmf_sequences":[{"digits":"8803","start_seconds":1.0,"end_seconds":2.0,"min_confidence":0.9}]}]}]}
    events=correlate_pcm_dtmf_with_sip(packet,pcm)
    assert events[0]['type']=='DTMF_SIP_DIAL_MATCH'
    assert events[0]['details']['sip_target']=='8803'
