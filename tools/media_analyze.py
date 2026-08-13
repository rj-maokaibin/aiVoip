#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from app.analyzers.media import MediaIntelligenceEngine
from app.analyzers.packet import TSharkAdapter
from app.analyzers.pcm.profile import load_pcm_profile


def main():
    ap=argparse.ArgumentParser(description='VOIP Media Intelligence offline analyzer')
    ap.add_argument('pcap')
    ap.add_argument('--profile',default=str(ROOT/'profiles/pcm/ruijie_aim_diag_v1.yaml'))
    ap.add_argument('--out-dir',default='media_artifacts')
    ap.add_argument('-o','--output',default='media_analysis.json')
    ap.add_argument('--tshark',default='tshark')
    args=ap.parse_args()
    eng=MediaIntelligenceEngine(load_pcm_profile(args.profile),TSharkAdapter(args.tshark,300))
    result=eng.analyze_pcap(args.pcap,args.out_dir)
    # Standalone result keeps filenames but should not expose machine-local absolute paths.
    for a in result.get('artifacts',[]): a.pop('local_path',None)
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result.get('summary',{}),ensure_ascii=False,indent=2))
    if result.get('status')=='PARTIAL_SUCCESS': print('PARTIAL_SUCCESS:',result.get('degraded_reason'))

if __name__=='__main__': main()
