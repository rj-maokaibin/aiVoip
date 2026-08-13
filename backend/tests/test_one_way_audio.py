from app.analyzers.packet.engine import PacketIntelligenceEngine
from app.analyzers.packet.types import NormalizedPacket, RtpData, SdpData, SipData
from app.diagnosis.reasoner import DeterministicDiagnosisReasoner

OFFER='''v=0\r\nc=IN IP4 10.0.0.1\r\nm=audio 4000 RTP/AVP 8\r\na=rtpmap:8 PCMA/8000\r\na=ptime:20\r\na=sendrecv\r\n'''
ANSWER='''v=0\r\nc=IN IP4 10.0.0.2\r\nm=audio 5000 RTP/AVP 8\r\na=rtpmap:8 PCMA/8000\r\na=ptime:20\r\na=sendrecv\r\n'''

def sip(frame,t,src,dst,method=None,status=None,sdp=None):
    return NormalizedPacket(frame_number=frame,timestamp=t,src_ip=src,dst_ip=dst,transport='UDP',src_port=5060,dst_port=5060,
        sip=SipData(method=method,status_code=status,call_id='ow',cseq=1,cseq_method='INVITE',from_uri='sip:1001@pbx',to_uri='sip:1002@pbx'),
        sdp=SdpData(raw=sdp) if sdp else None)

def rtp(frame,t,seq):
    return NormalizedPacket(frame_number=frame,timestamp=t,src_ip='10.0.0.1',dst_ip='10.0.0.2',transport='UDP',src_port=4000,dst_port=5000,
        rtp=RtpData(ssrc=11,sequence=seq,timestamp=(seq-1)*160,payload_type=8))

def packets(bidirectional=False):
    out=[sip(1,1,'10.0.0.1','10.0.0.2',method='INVITE',sdp=OFFER),sip(2,1.1,'10.0.0.2','10.0.0.1',status=200,sdp=ANSWER),sip(3,1.11,'10.0.0.1','10.0.0.2',method='ACK')]
    for i in range(30): out.append(rtp(10+i,1.2+i*.02,i+1))
    if bidirectional:
        for i in range(30):
            out.append(NormalizedPacket(frame_number=100+i,timestamp=1.2+i*.02,src_ip='10.0.0.2',dst_ip='10.0.0.1',transport='UDP',src_port=5000,dst_port=4000,rtp=RtpData(ssrc=22,sequence=i+1,timestamp=i*160,payload_type=8)))
    return out

def test_one_way_is_detected_only_with_complete_sip_and_meaningful_rtp():
    result=PacketIntelligenceEngine().analyze_packets(packets())
    assert any(x['type']=='ONE_WAY_RTP_MEDIA' for x in result['anomalies'])
    health=result['calls'][0]['media_direction_health']
    assert health['status']=='ONE_WAY_A_TO_B' and health['a_to_b_packets']==30 and health['b_to_a_packets']==0
    snap={'case':{'id':'c','summary':'通话单通，对方听不到'},'devices':[],'evidences':[{'id':'e','type':'PCAP','filename':'x.pcap'}],
          'analyzers':{'packet_intelligence':{'run_id':'r','status':'SUCCESS','version':'x','result':result}},'fingerprint':'x'}
    d=DeterministicDiagnosisReasoner().reason(snap)
    h=next(x for x in d.hypotheses if x.code=='ONE_WAY_AUDIO_PATH')
    assert h.status=='SUPPORTED' and h.confirmable is False
    assert any(a.action_type=='REQUEST_MULTI_POINT_PCAP' for a in d.plan)

def test_normal_bidirectional_call_is_not_misclassified():
    result=PacketIntelligenceEngine().analyze_packets(packets(True))
    assert not any(x['type']=='ONE_WAY_RTP_MEDIA' for x in result['anomalies'])
    assert result['calls'][0]['media_direction_health']['status']=='BIDIRECTIONAL'

def test_rtp_from_other_time_window_does_not_hide_one_way_call():
    ps=packets(False)
    # Reverse RTP reuses the same endpoints but occurs far outside this call's media window.
    for i in range(30):
        ps.append(NormalizedPacket(frame_number=300+i,timestamp=100.0+i*.02,src_ip='10.0.0.2',dst_ip='10.0.0.1',transport='UDP',src_port=5000,dst_port=4000,rtp=RtpData(ssrc=99,sequence=i+1,timestamp=i*160,payload_type=8)))
    result=PacketIntelligenceEngine().analyze_packets(ps)
    assert result['calls'][0]['media_direction_health']['status']=='ONE_WAY_A_TO_B'
    assert any(x['type']=='ONE_WAY_RTP_MEDIA' for x in result['anomalies'])
