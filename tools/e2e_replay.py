#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
import numpy as np, yaml
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from app.analyzers.packet.engine import PacketIntelligenceEngine
from app.analyzers.packet.types import NormalizedPacket,SipData,SdpData,RtpData
from app.analyzers.packet.rtp import RtpStreamAnalyzer
from app.analyzers.pcm.signal import detect_dtmf, dtmf_sequence
from app.analyzers.correlation import correlate_pcm_dtmf_with_sip
from app.analyzers.audio.quality import detect_unexpected_silence, detect_click_pop_robust, analyze_echo_path
from app.diagnosis.reasoner import DeterministicDiagnosisReasoner
from app.rules.compiler import load_rule_yaml
from app.rules.engine import RuleEngine

OFFER='''v=0\r\nc=IN IP4 10.0.0.1\r\nm=audio 4000 RTP/AVP 8 0\r\na=rtpmap:8 PCMA/8000\r\na=rtpmap:0 PCMU/8000\r\na=ptime:20\r\na=sendrecv\r\n'''
ANSWER_PCMA='''v=0\r\nc=IN IP4 10.0.0.2\r\nm=audio 5000 RTP/AVP 8\r\na=rtpmap:8 PCMA/8000\r\na=ptime:20\r\na=sendrecv\r\n'''

def sip(frame,t,src,dst,call='c',method=None,status=None,cseq_method=None,sdp=None,cseq=1,to='sip:1002@pbx'):
    return NormalizedPacket(frame_number=frame,timestamp=t,src_ip=src,dst_ip=dst,transport='UDP',src_port=5060,dst_port=5060,
        sip=SipData(method=method,status_code=status,call_id=call,cseq=cseq,cseq_method=cseq_method or method,from_uri='sip:1001@pbx',to_uri=to),sdp=SdpData(raw=sdp) if sdp else None)
def rtp(frame,t,src,sp,dst,dp,seq,ts,pt=8,ssrc=1):
    return NormalizedPacket(frame_number=frame,timestamp=t,src_ip=src,dst_ip=dst,transport='UDP',src_port=sp,dst_port=dp,rtp=RtpData(ssrc=ssrc,sequence=seq,timestamp=ts,payload_type=pt,payload_hex='d5'*160))
def established_base(call='c',answer=ANSWER_PCMA):
    return [sip(1,1,'10.0.0.1','10.0.0.2',call,method='INVITE',sdp=OFFER),sip(2,1.1,'10.0.0.2','10.0.0.1',call,status=200,cseq_method='INVITE',sdp=answer),sip(3,1.11,'10.0.0.1','10.0.0.2',call,method='ACK',cseq_method='INVITE')]
def bidir_media(pt=8,gap=False,one_way=False):
    out=[]; seq=1; ts=0
    for i in range(30):
        if gap and i==10: seq+=4; ts+=4*160
        out.append(rtp(10+i,1.2+i*.02,'10.0.0.1',4000,'10.0.0.2',5000,seq,ts,pt,11)); seq+=1; ts+=160
    if not one_way:
        for i in range(30): out.append(rtp(100+i,1.2+i*.02,'10.0.0.2',5000,'10.0.0.1',4000,i+1,i*160,pt,22))
    return out

def tone(digit,sr=8000,on_ms=100,off_ms=100):
    tab={'8':(852,1336),'0':(941,1336),'3':(697,1477)}; f1,f2=tab[digit]; n=int(sr*on_ms/1000); t=np.arange(n)/sr
    return np.r_[(3500*np.sin(2*np.pi*f1*t)+3500*np.sin(2*np.pi*f2*t)).astype(np.int16),np.zeros(int(sr*off_ms/1000),dtype=np.int16)]
def speech(sr=8000,seconds=3,seed=4):
    rng=np.random.default_rng(seed); n=int(sr*seconds); y=np.convolve(rng.normal(size=n),np.ones(5)/5,'same'); y/=np.max(np.abs(y))+1e-9; return (y*9000).astype(np.int16)

def fixture(kind):
    if kind=='sip_registration_failed':
        pk=[sip(1,1,'10.0.0.1','10.0.0.2','reg',method='REGISTER',to='sip:1001@pbx'),sip(2,1.01,'10.0.0.2','10.0.0.1','reg',status=401,cseq_method='REGISTER'),sip(3,1.02,'10.0.0.1','10.0.0.2','reg',method='REGISTER',cseq=2,to='sip:1001@pbx'),sip(4,1.03,'10.0.0.2','10.0.0.1','reg',status=403,cseq_method='REGISTER',cseq=2)]
        return {'packet':PacketIntelligenceEngine().analyze_packets(pk),'correlations':[],'cross_layer_events':[]}
    if kind=='sip_call_404':
        pk=[sip(1,1,'10.0.0.1','10.0.0.2',method='INVITE'),sip(2,1.02,'10.0.0.2','10.0.0.1',status=100,cseq_method='INVITE'),sip(3,1.05,'10.0.0.2','10.0.0.1',status=404,cseq_method='INVITE'),sip(4,1.06,'10.0.0.1','10.0.0.2',method='ACK',cseq_method='INVITE')]
        return {'packet':PacketIntelligenceEngine().analyze_packets(pk),'correlations':[],'cross_layer_events':[]}
    if kind=='one_way_audio':
        return {'packet':PacketIntelligenceEngine().analyze_packets(established_base()+bidir_media(one_way=True)),'correlations':[],'cross_layer_events':[]}
    if kind=='codec_mismatch':
        return {'packet':PacketIntelligenceEngine().analyze_packets(established_base()+bidir_media(pt=0)),'correlations':[],'cross_layer_events':[]}
    if kind=='rtp_burst_loss':
        return {'packet':PacketIntelligenceEngine().analyze_packets(established_base()+bidir_media(gap=True)),'correlations':[],'cross_layer_events':[]}
    if kind=='dtmf_first_digit_loss':
        sr=8000; x=np.concatenate([tone('8'),tone('8'),tone('0'),tone('3')]); seq=dtmf_sequence(detect_dtmf(x,sr))
        pcm={'streams':[{'tap':{'name':'pcm_rx','direction':'RX'},'sessions':[{'session_index':0,'start_time':100.0,'dtmf_sequences':seq}]}]}; packet={'calls':[{'call_id':'d','start_time':101,'callee':'sip:803@pbx'}],'anomalies':[],'registrations':[],'rtp_streams':[]}
        cross=correlate_pcm_dtmf_with_sip(packet,pcm,15); return {'packet':packet,'pcm':pcm,'correlations':[],'cross_layer_events':cross}
    if kind=='echo':
        sr=8000; ref=speech(sr,5,11).astype(float); delay=int(.086*sr); obs=np.random.default_rng(12).normal(0,220,len(ref)); obs[delay:]+=.62*ref[:-delay]; er=analyze_echo_path(ref.astype(np.int16),np.clip(obs,-32768,32767).astype(np.int16),sr,min_delay_ms=30,max_delay_ms=180)
        return {'packet':{'anomalies':[],'calls':[],'registrations':[],'rtp_streams':[]},'pcm':{'streams':[]},'correlations':[],'cross_layer_events':[{'type':'ECHO_PATH_DETECTED','time':1,'details':er}]}
    if kind=='click_pop':
        sr=8000; t=np.arange(sr*2)/sr; x=(5000*np.sin(2*np.pi*440*t)).astype(np.int16); x[sr:sr+3]=[30000,-30000,28000]; ev=detect_click_pop_robust(x,sr)
        return {'packet':{'anomalies':[],'calls':[],'registrations':[],'rtp_streams':[]},'pcm':{'streams':[]},'correlations':[],'cross_layer_events':[{'type':'CLICK_POP','time':e['time_seconds'],'details':e} for e in ev]}
    if kind=='silence':
        sr=8000; x=speech(sr,3); x[int(1.2*sr):int(1.55*sr)]=0; ev=detect_unexpected_silence(x,sr,min_duration_ms=200)
        return {'packet':{'anomalies':[],'calls':[],'registrations':[],'rtp_streams':[]},'pcm':{'streams':[]},'correlations':[],'cross_layer_events':[{'type':'UNEXPECTED_SILENCE','time':e['start_seconds'],'details':e} for e in ev]}
    if kind=='normal_call':
        return {'packet':PacketIntelligenceEngine().analyze_packets(established_base()+bidir_media()),'correlations':[],'cross_layer_events':[]}
    raise ValueError(kind)

def rules():
    return [load_rule_yaml(p.read_text()) for p in sorted((ROOT/'rules/diagnosis').glob('*.yaml'))]
def observed(manifest,result):
    snap={'case':{'id':manifest['id'],'summary':manifest['symptom']},'devices':[],'evidences':[{'id':'e','type':'PCAP','filename':'fixture.pcap'}], 'analyzers':{'media_intelligence':{'run_id':'run','status':'SUCCESS','version':'e2e','result':result}},'fingerprint':'e2e'}
    d=DeterministicDiagnosisReasoner().reason(snap); effects,matches,facts=RuleEngine().evaluate(snap,rules())
    packet=result.get('packet',result); anomalies=[x.get('type') for x in packet.get('anomalies',[])]; hyps={h.code:h.status for h in d.hypotheses}
    return {'anomaly_types':anomalies,'call_states':[x.get('state') for x in packet.get('calls',[])],'registration_states':[x.get('status') for x in packet.get('registrations',[])], 'media_direction_statuses':[(x.get('media_direction_health') or {}).get('status') for x in packet.get('calls',[])], 'hypothesis_codes':list(hyps),'hypothesis_statuses':hyps,'confirmed_hypotheses':[k for k,v in hyps.items() if v=='CONFIRMED'],'matched_rules':[m.rule_key for m in matches if m.matched], 'plan_actions':[a.action_type for a in d.plan], 'conclusion_state':d.conclusion_state,'rtp_stream_count':len(packet.get('rtp_streams',[])), 'facts':facts,'diagnosis_summary':d.summary}
def check_assertions(obs,exp):
    checks=[]
    for k,v in (exp.get('equals') or {}).items(): checks.append({'name':f'{k}=','passed':obs.get(k)==v,'actual':obs.get(k),'expected':v})
    for k,vals in (exp.get('contains') or {}).items():
        actual=obs.get(k,[]); vals=vals if isinstance(vals,list) else [vals]
        for v in vals: checks.append({'name':f'{k} contains {v}','passed':v in actual,'actual':actual,'expected':v})
    for k,vals in (exp.get('not_contains') or {}).items():
        actual=obs.get(k,[]); vals=vals if isinstance(vals,list) else [vals]
        for v in vals: checks.append({'name':f'{k} excludes {v}','passed':v not in actual,'actual':actual,'expected':f'not {v}'})
    for code,status in (exp.get('hypothesis_status') or {}).items(): checks.append({'name':f'hypothesis {code}','passed':obs['hypothesis_statuses'].get(code)==status,'actual':obs['hypothesis_statuses'].get(code),'expected':status})
    return checks

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--root',type=Path,default=ROOT/'e2e_cases'); ap.add_argument('--result',type=Path,default=Path('.e2e-result.json')); args=ap.parse_args()
    cases=[]
    for p in sorted(args.root.glob('*.yaml')):
        m=yaml.safe_load(p.read_text()); result=fixture(m['fixture']); obs=observed(m,result); checks=check_assertions(obs,m['expected']); cases.append({'id':m['id'],'name':m['name'],'passed':all(x['passed'] for x in checks),'checks':checks,'observed':obs})
    out={'passed':all(c['passed'] for c in cases),'case_count':len(cases),'checks_total':sum(len(c['checks']) for c in cases),'checks_passed':sum(sum(x['passed'] for x in c['checks']) for c in cases),'cases':cases}
    args.result.write_text(json.dumps(out,ensure_ascii=False,indent=2)); print(json.dumps({k:out[k] for k in ('passed','case_count','checks_passed','checks_total')},ensure_ascii=False,indent=2)); return 0 if out['passed'] else 1
if __name__=='__main__': raise SystemExit(main())
