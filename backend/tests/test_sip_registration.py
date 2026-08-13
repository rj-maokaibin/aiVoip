from app.analyzers.packet.sip import reconstruct_sip
from app.analyzers.packet.types import NormalizedPacket, SipData


def p(frame,t,src,dst,method=None,status=None):
    return NormalizedPacket(frame_number=frame,timestamp=t,src_ip=src,dst_ip=dst,transport='UDP',src_port=5060,dst_port=5060,
        sip=SipData(method=method,status_code=status,call_id='reg',cseq=1 if frame<3 else 2,cseq_method='REGISTER',from_uri='sip:1001@pbx',to_uri='sip:1001@pbx'))


def test_401_then_authenticated_register_200_is_success():
    result=reconstruct_sip([
        p(1,1.0,'10.0.0.1','10.0.0.2',method='REGISTER'),
        p(2,1.01,'10.0.0.2','10.0.0.1',status=401),
        p(3,1.02,'10.0.0.1','10.0.0.2',method='REGISTER'),
        p(4,1.03,'10.0.0.2','10.0.0.1',status=200),
    ])
    reg=result['registrations'][0]
    assert reg['status']=='SUCCESS'
    assert reg['auth_challenges']==1
    semantic=reg['ladder'][1]['semantic']
    assert semantic['is_expected'] is True
