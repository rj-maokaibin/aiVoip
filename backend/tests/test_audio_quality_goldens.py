from __future__ import annotations

import numpy as np

from app.analyzers.audio.quality import detect_unexpected_silence, detect_click_pop_robust, analyze_echo_path
from app.analyzers.pcm.signal import detect_dtmf, dtmf_sequence
from app.analyzers.correlation import correlate_pcm_dtmf_with_sip
from app.analyzers.packet.rtp import RtpStreamAnalyzer
from app.analyzers.packet.types import NormalizedPacket, RtpData
from app.diagnosis.reasoner import DeterministicDiagnosisReasoner


def _speech_like(sr: int, seconds: float, seed: int = 7) -> np.ndarray:
    rng=np.random.default_rng(seed)
    n=int(sr*seconds)
    # band-ish deterministic broadband signal with slowly varying envelope
    white=rng.normal(0,1,n)
    y=np.convolve(white,np.ones(5)/5,mode='same')
    t=np.arange(n)/sr
    env=0.55+0.35*np.sin(2*np.pi*2.3*t)**2
    y=y/(np.max(np.abs(y))+1e-12)*9000*env
    return np.clip(y,-30000,30000).astype(np.int16)


def test_unexpected_silence_requires_active_context():
    sr=8000
    x=_speech_like(sr,3.0)
    x[int(1.20*sr):int(1.55*sr)]=0
    events=detect_unexpected_silence(x,sr,min_duration_ms=200)
    assert len(events)==1
    assert 320 <= events[0]['duration_ms'] <= 380
    # All-silence track has no voiced context, so it is not promoted as an interruption.
    assert detect_unexpected_silence(np.zeros(sr*2,dtype=np.int16),sr,min_duration_ms=200)==[]


def test_click_pop_robust_rejects_clean_tone_and_detects_impulse():
    sr=8000; t=np.arange(sr*2)/sr
    clean=(5000*np.sin(2*np.pi*440*t)).astype(np.int16)
    assert detect_click_pop_robust(clean,sr)==[]
    x=clean.copy(); i=sr; x[i]=30000; x[i+1]=-30000; x[i+2]=28000
    events=detect_click_pop_robust(x,sr)
    assert events
    assert abs(events[0]['time_seconds']-1.0)<0.02
    assert events[0]['energy_rise_db']>=7.0


def test_echo_detector_recovers_known_delay():
    sr=8000
    ref=_speech_like(sr,5.0,seed=11).astype(np.float64)
    delay=int(0.086*sr)
    obs=np.random.default_rng(12).normal(0,220,len(ref))
    obs[delay:]+=0.62*ref[:-delay]
    obs=np.clip(obs,-32768,32767).astype(np.int16)
    r=analyze_echo_path(ref.astype(np.int16),obs,sr,min_delay_ms=30,max_delay_ms=180)
    assert r['detected'] is True
    assert abs(r['delay_ms']-86)<=2
    assert r['absolute_correlation']>0.75


def _rtp(seq,ts,t):
    return NormalizedPacket(frame_number=seq,timestamp=t,src_ip='1.1.1.1',dst_ip='2.2.2.2',transport='UDP',src_port=10000,dst_port=20000,protocols=['rtp'],rtp=RtpData(ssrc=1,sequence=seq,timestamp=ts,payload_type=8,payload_hex='d5'*160))


def test_rtp_burst_loss_golden_is_80ms():
    packets=[]
    # Expected sequence/timestamp cadence is 20 ms. Seqs 102-105 are missing.
    for seq,ts,t in [(100,0,1.00),(101,160,1.02),(106,960,1.12),(107,1120,1.14)]:
        packets.append(_rtp(seq,ts,t))
    result=RtpStreamAnalyzer().analyze(packets)[0]
    bursts=[e for e in result['events'] if e['type']=='BURST_LOSS']
    assert result['lost_packets']==4
    assert result['max_consecutive_loss']==4
    assert len(bursts)==1
    assert bursts[0]['details']['estimated_audio_loss_ms']==80.0


def _tone(digit: str, sr=8000, on_ms=100, off_ms=100):
    table={'8':(852,1336),'0':(941,1336),'3':(697,1477),'2':(697,1336)}
    f1,f2=table[digit]; n=int(sr*on_ms/1000); t=np.arange(n)/sr
    x=(3500*np.sin(2*np.pi*f1*t)+3500*np.sin(2*np.pi*f2*t)).astype(np.int16)
    return np.r_[x,np.zeros(int(sr*off_ms/1000),dtype=np.int16)]


def test_dtmf_mismatch_becomes_supported_hypothesis():
    sr=8000; samples=np.concatenate([_tone('8'),_tone('8'),_tone('0'),_tone('3')])
    events=detect_dtmf(samples,sr); seq=dtmf_sequence(events)
    assert seq[0]['digits']=='8803'
    pcm={'streams':[{'tap':{'name':'pcm_rx','direction':'RX'},'sessions':[{'session_index':0,'start_time':100.0,'dtmf_sequences':seq}]}]}
    # Deliberately missing first 8 in SIP target.
    packet={'calls':[{'call_id':'c1','start_time':101.0,'callee':'sip:803@pbx'}]}
    cross=correlate_pcm_dtmf_with_sip(packet,pcm,lookback_seconds=15)
    assert cross and cross[0]['type']=='DTMF_SIP_DIAL_MISMATCH'
    result={'packet':{'anomalies':[],'calls':packet['calls'],'registrations':[],'rtp_streams':[]},'pcm':pcm,'correlations':[],'cross_layer_events':cross}
    snap={'case':{'id':'c','summary':'重启后首次拨号丢号'},'devices':[],'evidences':[{'id':'e','type':'PCAP','filename':'x.pcap'}],'analyzers':{'media_intelligence':{'run_id':'r1','result':result,'status':'SUCCESS','version':'x'}},'fingerprint':'x'}
    d=DeterministicDiagnosisReasoner().reason(snap)
    h=next(h for h in d.hypotheses if h.code=='DTMF_DIGIT_ASSEMBLY_MISMATCH')
    assert h.status=='SUPPORTED'
    assert h.confidence>=0.9


def test_echo_event_reasoner_does_not_confirm_specific_root():
    event={'type':'ECHO_PATH_DETECTED','time':1.0,'details':{'absolute_correlation':0.82,'delay_ms':86.0,'evidence_level':'L2'}}
    result={'packet':{'anomalies':[],'calls':[],'registrations':[],'rtp_streams':[]},'pcm':{'streams':[]},'correlations':[],'cross_layer_events':[event]}
    snap={'case':{'id':'c','summary':'通话有明显回声'},'devices':[],'evidences':[{'id':'e','type':'PCAP','filename':'x.pcap'}],'analyzers':{'media_intelligence':{'run_id':'r1','result':result,'status':'SUCCESS','version':'x'}},'fingerprint':'x'}
    d=DeterministicDiagnosisReasoner().reason(snap)
    h=next(h for h in d.hypotheses if h.code=='AUDIO_ECHO_PATH')
    assert h.status=='SUPPORTED'
    assert h.confirmable is False
    assert not any(x.status=='CONFIRMED' for x in d.hypotheses)


def test_hum_candidate_requires_noise_symptom_before_becoming_supported_fault():
    result={
        'packet':{'anomalies':[],'calls':[],'registrations':[],'rtp_streams':[]},
        'pcm':{'streams':[{'sessions':[{'hum':{'level':'HIGH'}}]}]},
        'correlations':[], 'cross_layer_events':[],
    }
    base={'devices':[],'evidences':[{'id':'e','type':'PCAP','filename':'x.pcap'}],
          'analyzers':{'media_intelligence':{'run_id':'r1','result':result,'status':'SUCCESS','version':'x'}},
          'fingerprint':'x'}

    generic=DeterministicDiagnosisReasoner().reason({**base,'case':{'id':'c','summary':'例行通话验证'}})
    assert not any(h.code=='PCM_HUM_INTERFERENCE' for h in generic.hypotheses)
    assert any('仅保留为频谱候选' in item for item in generic.known)

    noisy=DeterministicDiagnosisReasoner().reason({**base,'case':{'id':'c','summary':'通话有明显电流音'}})
    hum=next(h for h in noisy.hypotheses if h.code=='PCM_HUM_INTERFERENCE')
    assert hum.status=='SUPPORTED'

from app.analyzers.media.engine import MediaIntelligenceEngine
from app.analyzers.pcm.profile import PcmProfile, PcmTap


def _engine():
    profile=PcmProfile(id='test',sample_rate=8000,bit_depth=16,signed=True,endian='little',channels=1,packet_payload_bytes=160,expected_packet_interval_ms=10,session_gap_ms=100,taps=[PcmTap(name='pcm_rx',direction='RX',dst_port=40000),PcmTap(name='pcm_tx',direction='TX',dst_port=50000)])
    return MediaIntelligenceEngine(profile)


def test_media_engine_scopes_silence_to_active_call_window():
    sr=8000; x=_speech_like(sr,4.0); x[int(1.5*sr):int(1.85*sr)]=0
    pcm=[{'tap':{'name':'pcm_rx','direction':'RX'},'session_index':0,'start_time':100.0,'end_time':104.0,'samples':x,'sample_rate':sr}]
    packet={'calls':[{'call_id':'c1','media_start_time':100.5,'media_end_time':103.5}]}
    events=_engine()._active_media_audio_events(pcm,packet)
    sil=[e for e in events if e['type']=='UNEXPECTED_SILENCE']
    assert len(sil)==1
    assert sil[0]['scope']['call_id']=='c1'
    assert 101.45 <= sil[0]['time'] <= 101.55


def test_media_engine_emits_echo_path_between_tx_and_rx():
    sr=8000; ref=_speech_like(sr,5.0,seed=21).astype(np.float64); delay=int(0.09*sr)
    obs=np.random.default_rng(22).normal(0,180,len(ref)); obs[delay:]+=0.65*ref[:-delay]; obs=np.clip(obs,-32768,32767).astype(np.int16)
    pcm=[
        {'tap':{'name':'pcm_tx','direction':'TX'},'session_index':0,'start_time':100.0,'end_time':105.0,'samples':ref.astype(np.int16),'sample_rate':sr},
        {'tap':{'name':'pcm_rx','direction':'RX'},'session_index':0,'start_time':100.0,'end_time':105.0,'samples':obs,'sample_rate':sr},
    ]
    events=_engine()._pcm_echo_events(pcm)
    assert events
    assert events[0]['type']=='ECHO_PATH_DETECTED'
    assert abs(events[0]['details']['delay_ms']-90)<=2
