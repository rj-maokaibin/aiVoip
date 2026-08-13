#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json, os, sys
from pathlib import Path
import yaml

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))

from app.analyzers.media.engine import MediaIntelligenceEngine
from app.analyzers.pcm.profile import load_pcm_profile
from app.analyzers.packet.tshark import TSharkAdapter
from app.diagnosis.reasoner import DeterministicDiagnosisReasoner


def sha256(path:Path)->str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()


def validate(result:dict, manifest:dict)->list[dict]:
    checks=[]; exp=manifest['expected']; paths=result.get('periodic_interference_paths',[]) or []
    def add(name,ok,actual,expected): checks.append({'name':name,'passed':bool(ok),'actual':actual,'expected':expected})
    add('periodic_interference_count',sum(1 for x in paths if x.get('type')=='LOCAL_CAPTURE_PERIODIC_INTERFERENCE')>=exp['periodic_interference_count_min'],sum(1 for x in paths if x.get('type')=='LOCAL_CAPTURE_PERIODIC_INTERFERENCE'),f">={exp['periodic_interference_count_min']}")
    by_session={int(x.get('scope',{}).get('pcm_session_index',-1)):x for x in paths}
    for pexp in exp.get('paths',[]):
        idx=int(pexp['pcm_session_index']); ev=by_session.get(idx); prefix=f'pcm_rx_session_{idx}'
        add(prefix+'_exists',ev is not None,bool(ev),True)
        if not ev: continue
        d=ev['details']
        pcm=(d.get('pcm_rx',{}).get('representative') or {}).get('autocorrelation') or {}
        up=(d.get('upstream_rtp',{}).get('representative') or {}).get('autocorrelation') or {}
        down=(d.get('downstream_rtp',{}).get('representative') or {}).get('autocorrelation') or {}
        hits=(d.get('pcm_rx',{}).get('comb') or {}).get('hit_count',0)
        add(prefix+'_pcm_ac20',float(pcm.get('20ms',0))>=pexp['pcm_rx_ac20_min'],pcm.get('20ms'),f">={pexp['pcm_rx_ac20_min']}")
        add(prefix+'_pcm_ac10',float(pcm.get('10ms',1))<=pexp['pcm_rx_ac10_max'],pcm.get('10ms'),f"<={pexp['pcm_rx_ac10_max']}")
        add(prefix+'_up_ac20',float(up.get('20ms',0))>=pexp['upstream_ac20_min'],up.get('20ms'),f">={pexp['upstream_ac20_min']}")
        add(prefix+'_down_ac20',float(down.get('20ms',1))<=pexp['downstream_ac20_max'],down.get('20ms'),f"<={pexp['downstream_ac20_max']}")
        add(prefix+'_comb_hits',int(hits)>=pexp['odd_50hz_comb_hits_min'],hits,f">={pexp['odd_50hz_comb_hits_min']}")
    return checks


def main():
    ap=argparse.ArgumentParser(description='Replay an Audio Golden Case against Media Intelligence')
    ap.add_argument('pcap',type=Path)
    ap.add_argument('--manifest',type=Path,default=ROOT/'golden_cases/APF1250_CS20260807_6886043/manifest.yaml')
    ap.add_argument('--profile',type=Path,default=ROOT/'profiles/pcm/ruijie_aim_diag_v1.yaml')
    ap.add_argument('--out-dir',type=Path,default=Path('golden-replay-artifacts'))
    ap.add_argument('--result',type=Path,default=Path('golden-replay-result.json'))
    ap.add_argument('--tshark',default=os.getenv('TSHARK_BINARY','tshark'))
    args=ap.parse_args()
    manifest=yaml.safe_load(args.manifest.read_text(encoding='utf-8'))
    actual_hash=sha256(args.pcap)
    if actual_hash!=manifest['source']['sha256']:
        print(json.dumps({'status':'FAILED','error':'SOURCE_SHA256_MISMATCH','actual':actual_hash,'expected':manifest['source']['sha256']},ensure_ascii=False,indent=2)); return 2
    profile=load_pcm_profile(args.profile)
    engine=MediaIntelligenceEngine(profile,TSharkAdapter(binary=args.tshark))
    result=engine.analyze_pcap(args.pcap,args.out_dir)
    checks=validate(result,manifest)
    diagnosis=DeterministicDiagnosisReasoner().reason({
        'case':{'id':'golden','summary':manifest.get('symptom','持续电流音')},
        'devices':[],'evidences':[{'id':'source','type':'PCAP','filename':args.pcap.name}],
        'analyzers':{'media_intelligence':{'run_id':'golden-run','status':result.get('status'),'version':result.get('version'),'result':result}},
        'fingerprint':'golden',
    })
    expected_code=manifest['expected'].get('hypothesis_code')
    expected_status=manifest['expected'].get('hypothesis_status')
    hyp=next((h for h in diagnosis.hypotheses if h.code==expected_code),None)
    checks.append({'name':'diagnosis_hypothesis','passed':bool(hyp and hyp.status==expected_status),'actual':{'code':hyp.code,'status':hyp.status,'confidence':hyp.confidence} if hyp else None,'expected':{'code':expected_code,'status':expected_status}})
    checks.append({'name':'specific_hardware_not_confirmed','passed':not any(h.status=='CONFIRMED' and h.code in {'POWER_SUPPLY_NOISE','GROUNDING_NOISE','PHONE_LINE_NOISE','FXS_SLIC_NOISE'} for h in diagnosis.hypotheses),'actual':[{'code':h.code,'status':h.status} for h in diagnosis.hypotheses],'expected':'no specific hardware root CONFIRMED'})
    payload={'golden_case':manifest['id'],'analyzer':result.get('analyzer'),'version':result.get('version'),'analysis_status':result.get('status'),'checks':checks,'passed':all(c['passed'] for c in checks),'summary':result.get('summary'),'periodic_interference_paths':result.get('periodic_interference_paths',[]),'diagnosis':{'state':diagnosis.conclusion_state,'summary':diagnosis.summary,'known':diagnosis.known,'unknown':diagnosis.unknown,'excluded':diagnosis.excluded}}
    args.result.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':payload['passed'],'checks_passed':sum(c['passed'] for c in checks),'checks_total':len(checks),'result':str(args.result)},ensure_ascii=False,indent=2))
    return 0 if payload['passed'] else 1

if __name__=='__main__': raise SystemExit(main())
