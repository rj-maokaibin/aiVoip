import numpy as np

from app.analyzers.audio.rtp_audio import render_rtp_tracks
from app.analyzers.audio.features import waveform_envelope, spectrogram_data, detect_silence, detect_click_pop
from app.analyzers.media.correlation import correlate_tracks
from app.analyzers.packet.types import NormalizedPacket, RtpData


def pkt(seq, ts, t, payload=b'\xd5'*160):
    return NormalizedPacket(frame_number=seq,timestamp=t,src_ip='10.0.0.1',dst_ip='10.0.0.2',transport='UDP',src_port=10000,dst_port=20000,
                            protocols=['rtp'],rtp=RtpData(ssrc=123,sequence=seq,timestamp=ts,payload_type=8,payload_hex=payload.hex()))


def test_rtp_render_inserts_silence_for_sequence_gap():
    packets=[pkt(100,0,1.00),pkt(101,160,1.02),pkt(103,480,1.06)]
    stream_id='10.0.0.1:10000>10.0.0.2:20000/ssrc=123'
    stream=[{'stream_id':stream_id,'codec':'PCMA','ptime_ms':20}]
    tracks=render_rtp_tracks(packets,stream)
    assert len(tracks)==1
    assert tracks[0].inserted_loss_samples==160
    assert tracks[0].samples.size==640


def test_waveform_and_spectrogram_are_bounded():
    sr=8000; t=np.arange(sr*2)/sr; x=(12000*np.sin(2*np.pi*440*t)).astype(np.int16)
    w=waveform_envelope(x,sr,max_bins=100)
    s=spectrogram_data(x,sr,max_time_bins=32,max_freq_bins=32)
    assert len(w['bins']) <= 100
    assert len(s['times']) <= 32
    assert len(s['frequencies']) <= 32


def test_silence_and_click_detectors():
    sr=8000
    x=np.zeros(sr,dtype=np.int16)
    x[:sr//4]=2000
    x[sr//2]=30000; x[sr//2+1]=-30000
    sil=detect_silence(x,sr,min_duration_ms=80)
    clicks=detect_click_pop(x,sr)
    assert sil
    assert clicks


def test_pcm_rtp_correlation_finds_known_delay():
    sr=8000; t=np.arange(sr*3)/sr
    base=(10000*np.sin(2*np.pi*697*t)+7000*np.sin(2*np.pi*1209*t)).astype(np.int16)
    delay=int(0.03*sr)
    delayed=np.r_[np.zeros(delay,dtype=np.int16),base[:-delay]]
    c=correlate_tracks(base,sr,100.0,delayed,sr,100.0,max_lag_ms=100)
    assert c is not None
    assert c['absolute_correlation'] > 0.9
    assert abs(abs(c['lag_ms'])-30) <= 2
