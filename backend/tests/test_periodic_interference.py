import math
import numpy as np

from app.analyzers.audio.periodic import analyze_low_energy_periodicity
from app.analyzers.audio.rtp_audio import RenderedRtpTrack
from app.analyzers.media.periodic import build_periodic_path_analysis
from app.diagnosis.reasoner import DeterministicDiagnosisReasoner


def _periodic(sr=8000, seconds=4.0, amp=120.0):
    t=np.arange(int(sr*seconds))/sr
    x=np.zeros_like(t)
    for hz in range(150,1000,100):
        x += np.sin(2*np.pi*hz*t)
    x=x/max(1.0,float(np.max(np.abs(x))))*amp
    return np.round(x).astype(np.int16)


def _track(stream_id, src_ip,src_port,dst_ip,dst_port,samples,start=100.0):
    return RenderedRtpTrack(
        stream_id=stream_id,src_ip=src_ip,src_port=src_port,dst_ip=dst_ip,dst_port=dst_port,ssrc=1,
        codec='PCMA',sample_rate=8000,channels=1,start_time=start,end_time=start+len(samples)/8000,
        samples=samples,packet_count=100,inserted_loss_samples=0,missing_payload_packets=0,sequence_first=1,sequence_last=100,
    )


def test_low_energy_periodic_detector_recognizes_odd_50hz_comb():
    x=_periodic()
    r=analyze_low_energy_periodicity(x,8000)
    assert r['level']=='HIGH'
    ac=r['representative']['autocorrelation']
    assert ac['10ms'] < -0.90
    assert ac['20ms'] > 0.98
    assert ac['40ms'] > 0.98
    assert r['comb']['hit_count'] >= 8
    assert 19.5 <= r['estimated_period']['period_ms'] <= 20.5


def test_periodic_detector_does_not_label_random_noise_high():
    rng=np.random.default_rng(7)
    x=np.clip(rng.normal(0,120,8000*4),-32768,32767).astype(np.int16)
    r=analyze_low_energy_periodicity(x,8000)
    assert r['level']!='HIGH'


def test_cross_layer_periodic_path_localizes_capture_direction():
    up=_periodic(seconds=5,amp=100)
    pcm=up.copy()
    rng=np.random.default_rng(4)
    down=np.clip(rng.normal(0,80,len(up)),-32768,32767).astype(np.int16)
    up_track=_track('up','192.168.0.12',10000,'192.168.0.253',17074,up)
    down_track=_track('down','192.168.0.253',17074,'192.168.0.12',10000,down)
    pcm_signals=[{'tap':{'name':'pcm_rx','direction':'RX'},'session_index':7,'start_time':100.0,'end_time':105.0,'samples':pcm,'sample_rate':8000}]
    correlations=[{'type':'PCM_RTP_CORRELATION','details':{'pcm_tap':'pcm_rx','pcm_session_index':7,'rtp_stream_id':'up','correlation':{'absolute_correlation':0.99,'quality':'HIGH'}}}]
    packet={'calls':[{'call_id':'c1','rtp_stream_ids':['up','down']}]}
    out=build_periodic_path_analysis(pcm_signals,[up_track,down_track],correlations,packet)
    assert len(out)==1
    assert out[0]['type']=='LOCAL_CAPTURE_PERIODIC_INTERFERENCE'
    assert out[0]['scope']['call_id']=='c1'
    assert out[0]['details']['pcm_rx']['level']=='HIGH'
    assert out[0]['details']['upstream_rtp']['level']=='HIGH'
    assert out[0]['details']['downstream_rtp']['level']=='LOW'


def test_reasoner_prioritizes_periodic_capture_interference_for_noise():
    event={
        'type':'LOCAL_CAPTURE_PERIODIC_INTERFERENCE','time':1.0,'severity':'HIGH',
        'details':{
            'strength':{'pcm_rx':0.97,'upstream_rtp':0.89,'downstream_rtp':0.15},
            'pcm_rx':{'representative':{'autocorrelation':{'10ms':-0.92,'20ms':0.985,'40ms':0.98}},'comb':{'hit_count':9}},
            'upstream_rtp':{'representative':{'autocorrelation':{'10ms':-0.74,'20ms':0.85,'40ms':0.84}}},
            'downstream_rtp':{'representative':{'autocorrelation':{'10ms':0.02,'20ms':0.03,'40ms':0.01}}},
        },
    }
    result={'packet':{'anomalies':[{'type':'HIGH_DELTA','time':1,'severity':'MEDIUM','evidence':{}}], 'calls':[], 'registrations':[], 'rtp_streams':[{}]},'correlations':[],'cross_layer_events':[event],'pcm':{'streams':[]}}
    snap={'case':{'id':'c','summary':'现场持续电流音杂音'},'devices':[],'evidences':[{'id':'e','type':'PCAP','filename':'x.pcap'}], 'analyzers':{'media_intelligence':{'run_id':'r1','result':result,'status':'SUCCESS','version':'x'}},'fingerprint':'x'}
    d=DeterministicDiagnosisReasoner().reason(snap)
    top=d.summary['top_hypotheses'][0]
    assert top['code']=='LOCAL_CAPTURE_PERIODIC_INTERFERENCE'
    assert top['status']=='SUPPORTED'
    assert d.conclusion_state=='DIAGNOSED'
    assert any('PBX/下行网络' in x for x in d.excluded)
    assert any(a.params.get('purpose')=='close_specific_hardware_root_cause' for a in d.plan)
