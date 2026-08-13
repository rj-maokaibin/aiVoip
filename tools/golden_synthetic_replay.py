#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import yaml

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))

from app.analyzers.audio.quality import detect_unexpected_silence, detect_click_pop_robust, analyze_echo_path
from app.analyzers.correlation import correlate_pcm_dtmf_with_sip
from app.analyzers.packet.rtp import RtpStreamAnalyzer
from app.analyzers.packet.types import NormalizedPacket, RtpData
from app.analyzers.pcm.signal import detect_dtmf, dtmf_sequence
from app.analyzers.pcm.wav import write_wav
from app.diagnosis.reasoner import DeterministicDiagnosisReasoner


def speech_like(sr:int, seconds:float, seed:int=7)->np.ndarray:
    rng=np.random.default_rng(seed); n=int(sr*seconds)
    white=rng.normal(0,1,n); y=np.convolve(white,np.ones(5)/5,mode='same')
    t=np.arange(n)/sr; env=0.55+0.35*np.sin(2*np.pi*2.3*t)**2
    y=y/(np.max(np.abs(y))+1e-12)*9000*env
    return np.clip(y,-30000,30000).astype(np.int16)


def rtp_pkt(seq:int,ts:int,t:float)->NormalizedPacket:
    return NormalizedPacket(frame_number=seq,timestamp=t,src_ip='1.1.1.1',dst_ip='2.2.2.2',transport='UDP',src_port=10000,dst_port=20000,protocols=['rtp'],rtp=RtpData(ssrc=1,sequence=seq,timestamp=ts,payload_type=8,payload_hex='d5'*160))


def tone(digit:str,sr=8000,on_ms=100,off_ms=100)->np.ndarray:
    table={'8':(852,1336),'0':(941,1336),'3':(697,1477),'2':(697,1336)}
    f1,f2=table[digit]; n=int(sr*on_ms/1000); t=np.arange(n)/sr
    x=(3500*np.sin(2*np.pi*f1*t)+3500*np.sin(2*np.pi*f2*t)).astype(np.int16)
    return np.r_[x,np.zeros(int(sr*off_ms/1000),dtype=np.int16)]


def reason(summary:str,result:dict):
    snap={'case':{'id':'golden','summary':summary},'devices':[],'evidences':[{'id':'e','type':'SYNTHETIC','filename':'generated'}],
          'analyzers':{'media_intelligence':{'run_id':'golden-run','result':result,'status':'SUCCESS','version':'golden'}},'fingerprint':'golden'}
    return DeterministicDiagnosisReasoner().reason(snap)


def check(name,ok,actual,expected):
    return {'name':name,'passed':bool(ok),'actual':actual,'expected':expected}


def run_case(manifest:dict,out_dir:Path)->dict:
    kind=manifest['kind']; exp=manifest['expected']; checks=[]; payload={}
    case_dir=out_dir/manifest['id']; case_dir.mkdir(parents=True,exist_ok=True)
    if kind=='synthetic_rtp_burst_loss':
        packets=[rtp_pkt(100,0,1.00),rtp_pkt(101,160,1.02),rtp_pkt(106,960,1.12),rtp_pkt(107,1120,1.14)]
        r=RtpStreamAnalyzer().analyze(packets)[0]; bursts=[e for e in r['events'] if e['type']=='BURST_LOSS']
        checks += [
            check('lost_packets',r['lost_packets']==exp['lost_packets'],r['lost_packets'],exp['lost_packets']),
            check('max_consecutive_loss',r['max_consecutive_loss']==exp['max_consecutive_loss'],r['max_consecutive_loss'],exp['max_consecutive_loss']),
            check('burst_event',len(bursts)==1,len(bursts),1),
            check('audio_loss_ms',bool(bursts) and bursts[0]['details']['estimated_audio_loss_ms']==exp['estimated_audio_loss_ms'],bursts[0]['details']['estimated_audio_loss_ms'] if bursts else None,exp['estimated_audio_loss_ms']),
            check('ptime_ms',r['ptime_ms']==exp['ptime_ms'],r['ptime_ms'],exp['ptime_ms']),
        ]; payload={'rtp':r}
    elif kind=='synthetic_audio_silence':
        sr=8000; x=speech_like(sr,3.0); x[int(1.20*sr):int(1.55*sr)]=0
        write_wav(case_dir/'silence_350ms.wav',x,sr,1)
        events=detect_unexpected_silence(x,sr,min_duration_ms=200)
        fp=detect_unexpected_silence(np.zeros(sr*2,dtype=np.int16),sr,min_duration_ms=200)
        duration=events[0]['duration_ms'] if events else None
        checks += [
            check('event_count',len(events)==exp['event_count'],len(events),exp['event_count']),
            check('duration_range',duration is not None and exp['duration_ms_min']<=duration<=exp['duration_ms_max'],duration,[exp['duration_ms_min'],exp['duration_ms_max']]),
            check('all_silence_false_positive',len(fp)==exp['all_silence_false_positive_count'],len(fp),exp['all_silence_false_positive_count']),
        ]; payload={'events':events}
    elif kind=='synthetic_audio_click_pop':
        sr=8000; t=np.arange(sr*2)/sr; clean=(5000*np.sin(2*np.pi*440*t)).astype(np.int16); x=clean.copy(); i=sr; x[i]=30000; x[i+1]=-30000; x[i+2]=28000
        write_wav(case_dir/'click_pop.wav',x,sr,1); write_wav(case_dir/'clean_tone.wav',clean,sr,1)
        events=detect_click_pop_robust(x,sr); fp=detect_click_pop_robust(clean,sr); first=events[0] if events else {}
        checks += [
            check('event_count_min',len(events)>=exp['event_count_min'],len(events),f">={exp['event_count_min']}"),
            check('event_time',bool(events) and abs(first['time_seconds']-exp['event_time_seconds'])*1000<=exp['event_time_tolerance_ms'],first.get('time_seconds'),exp['event_time_seconds']),
            check('clean_false_positive',len(fp)==exp['clean_tone_false_positive_count'],len(fp),exp['clean_tone_false_positive_count']),
            check('energy_rise',bool(events) and first['energy_rise_db']>=exp['min_energy_rise_db'],first.get('energy_rise_db'),f">={exp['min_energy_rise_db']}"),
        ]; payload={'events':events,'clean_false_positive_events':fp}
    elif kind=='synthetic_dtmf_mismatch':
        sr=8000; x=np.concatenate([tone('8'),tone('8'),tone('0'),tone('3')]); write_wav(case_dir/'dtmf_8803.wav',x,sr,1)
        seq=dtmf_sequence(detect_dtmf(x,sr)); digits=seq[0]['digits'] if seq else ''
        pcm={'streams':[{'tap':{'name':'pcm_rx','direction':'RX'},'sessions':[{'session_index':0,'start_time':100.0,'dtmf_sequences':seq}]}]}
        packet={'calls':[{'call_id':'c1','start_time':101.0,'callee':'sip:803@pbx'}]}
        cross=correlate_pcm_dtmf_with_sip(packet,pcm,15); ev=cross[0] if cross else None
        result={'packet':{'anomalies':[],'calls':packet['calls'],'registrations':[],'rtp_streams':[]},'pcm':pcm,'correlations':[],'cross_layer_events':cross}
        d=reason(manifest['symptom'],result); h=next((h for h in d.hypotheses if h.code==exp['hypothesis_code']),None)
        checks += [
            check('pcm_digits',digits==exp['pcm_digits'],digits,exp['pcm_digits']),
            check('cross_event',bool(ev) and ev['type']==exp['cross_event_type'],ev['type'] if ev else None,exp['cross_event_type']),
            check('hypothesis',bool(h) and h.status==exp['hypothesis_status'],{'code':h.code,'status':h.status,'confidence':h.confidence} if h else None,{'code':exp['hypothesis_code'],'status':exp['hypothesis_status']}),
            check('not_confirmed',not any(hh.status=='CONFIRMED' for hh in d.hypotheses),[{'code':hh.code,'status':hh.status} for hh in d.hypotheses],'no CONFIRMED'),
        ]; payload={'pcm_sequences':seq,'cross':cross,'diagnosis':d.summary}
    elif kind=='synthetic_audio_echo':
        sr=8000; ref=speech_like(sr,5.0,11).astype(np.float64); delay=int(exp['delay_ms']*sr/1000); obs=np.random.default_rng(12).normal(0,220,len(ref)); obs[delay:]+=0.62*ref[:-delay]; obs=np.clip(obs,-32768,32767).astype(np.int16); ref=ref.astype(np.int16)
        write_wav(case_dir/'echo_reference.wav',ref,sr,1); write_wav(case_dir/'echo_observed_86ms.wav',obs,sr,1)
        er=analyze_echo_path(ref,obs,sr,min_delay_ms=30,max_delay_ms=180)
        event={'type':'ECHO_PATH_DETECTED','time':1.0,'details':er}
        result={'packet':{'anomalies':[],'calls':[],'registrations':[],'rtp_streams':[]},'pcm':{'streams':[]},'correlations':[],'cross_layer_events':[event] if er.get('detected') else []}
        d=reason(manifest['symptom'],result); h=next((h for h in d.hypotheses if h.code==exp['hypothesis_code']),None)
        checks += [
            check('echo_detected',er.get('detected') is True,er.get('detected'),True),
            check('delay',abs(float(er.get('delay_ms',-999))-exp['delay_ms'])<=exp['delay_tolerance_ms'],er.get('delay_ms'),exp['delay_ms']),
            check('correlation',float(er.get('absolute_correlation',0))>=exp['correlation_min'],er.get('absolute_correlation'),f">={exp['correlation_min']}"),
            check('hypothesis',bool(h) and h.status==exp['hypothesis_status'],{'code':h.code,'status':h.status,'confidence':h.confidence} if h else None,{'code':exp['hypothesis_code'],'status':exp['hypothesis_status']}),
            check('not_confirmed',not any(hh.status=='CONFIRMED' for hh in d.hypotheses),[{'code':hh.code,'status':hh.status} for hh in d.hypotheses],'no CONFIRMED'),
        ]; payload={'echo':er,'diagnosis':d.summary}
    else:
        raise ValueError(f'unsupported synthetic golden kind: {kind}')
    return {'id':manifest['id'],'name':manifest['name'],'kind':kind,'passed':all(c['passed'] for c in checks),'checks':checks,'payload':payload}


def main():
    ap=argparse.ArgumentParser(description='Run all synthetic VOIP Golden Cases')
    ap.add_argument('--root',type=Path,default=ROOT/'golden_cases')
    ap.add_argument('--out-dir',type=Path,default=Path('golden-synthetic-artifacts'))
    ap.add_argument('--result',type=Path,default=Path('golden-synthetic-result.json'))
    args=ap.parse_args(); args.out_dir.mkdir(parents=True,exist_ok=True)
    cases=[]
    for manifest_path in sorted(args.root.glob('*/manifest.yaml')):
        manifest=yaml.safe_load(manifest_path.read_text(encoding='utf-8'))
        if not str(manifest.get('kind','')).startswith('synthetic_'):
            continue
        cases.append(run_case(manifest,args.out_dir))
    result={'passed':all(c['passed'] for c in cases),'case_count':len(cases),'checks_total':sum(len(c['checks']) for c in cases),'checks_passed':sum(sum(1 for x in c['checks'] if x['passed']) for c in cases),'cases':cases}
    args.result.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:result[k] for k in ('passed','case_count','checks_passed','checks_total')},ensure_ascii=False,indent=2))
    return 0 if result['passed'] else 1

if __name__=='__main__': raise SystemExit(main())
