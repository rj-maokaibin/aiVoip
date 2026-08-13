from app.analyzers.packet.sip import reconstruct_sip
from app.analyzers.packet.types import NormalizedPacket, SipData

def p(n,t,method=None,status=None,cseq_method='INVITE'):
    return NormalizedPacket(frame_number=n,timestamp=t,src_ip='a',dst_ip='b',src_port=5060,dst_port=5060,
        sip=SipData(method=method,status_code=status,call_id='c1',cseq=1,cseq_method=cseq_method))

def test_successful_call_keeps_2xx_as_final_even_if_late_487_arrives():
    packets=[p(1,1.0,'INVITE'),p(2,2.0,status=200),p(3,2.1,'ACK'),p(4,10.0,'BYE',cseq_method='BYE'),p(5,10.1,status=200,cseq_method='BYE'),p(6,13.0,status=487)]
    call=reconstruct_sip(packets)['calls'][0]
    assert call['state']=='TERMINATED'
    assert call['invite_final_status']==200
    assert call['conflicting_final_responses'][0]['status_code']==487

def test_call_exposes_active_media_window_from_ack_to_bye():
    packets=[p(1,1.0,'INVITE'),p(2,2.0,status=200),p(3,2.1,'ACK'),p(4,10.0,'BYE',cseq_method='BYE'),p(5,10.1,status=200,cseq_method='BYE')]
    call=reconstruct_sip(packets)['calls'][0]
    assert call['media_start_time']==2.1
    assert call['media_end_time']==10.0
    assert call['active_media_duration_seconds']==7.9
