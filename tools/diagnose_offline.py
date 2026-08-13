#!/usr/bin/env python3
from __future__ import annotations
import argparse,json
from pathlib import Path
from app.diagnosis.factory import get_diagnosis_reasoner

def main():
    ap=argparse.ArgumentParser(description='Run VOIP M4 diagnosis reasoner against an AnalyzerResult JSON')
    ap.add_argument('analysis_json'); ap.add_argument('--summary',default='VOIP故障待诊断'); ap.add_argument('-o','--output',default='diagnosis.json')
    args=ap.parse_args(); result=json.loads(Path(args.analysis_json).read_text(encoding='utf-8'))
    analyzer=result.get('analyzer','media_intelligence'); version=result.get('version','unknown')
    snapshot={'case':{'id':'offline','case_no':'OFFLINE','summary':args.summary,'status':'ANALYZING'},'devices':[],
              'evidences':[{'id':'offline-evidence','type':'ANALYZER_INPUT','filename':Path(args.analysis_json).name,'sha256':'offline'}],
              'analyzers':{analyzer:{'run_id':'offline-run','status':result.get('status','SUCCESS'),'version':version,'summary':result.get('summary',{}),'result':result}},'fingerprint':'offline'}
    decision=get_diagnosis_reasoner().reason(snapshot).to_dict(); Path(args.output).write_text(json.dumps(decision,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'state':decision['conclusion_state'],'summary':decision['summary'],'known':decision['known'],'unknown':decision['unknown'],'plan':decision['plan']},ensure_ascii=False,indent=2))
if __name__=='__main__': main()
