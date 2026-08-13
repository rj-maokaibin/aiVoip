#!/usr/bin/env python3
from __future__ import annotations
import argparse,json,subprocess,sys,tempfile
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT / 'tools') not in sys.path: sys.path.insert(0, str(ROOT / 'tools'))
from release_evidence import evidence_envelope

def main():
    ap=argparse.ArgumentParser(description='Replay all field Golden Cases from an external evidence directory')
    ap.add_argument('--evidence-dir',type=Path,required=True); ap.add_argument('--out',type=Path,default=Path('.field-golden-result.json')); ap.add_argument('--require-all',action='store_true'); args=ap.parse_args()
    cases=[]; failed=skipped=0
    for mp in sorted((ROOT/'golden_cases').glob('*/manifest.yaml')):
        m=yaml.safe_load(mp.read_text(encoding='utf-8')); source=m.get('source') or {}
        if not source.get('filename'): continue
        evidence=args.evidence_dir/source['filename']; row={'id':m['id'],'manifest':str(mp.relative_to(ROOT)),'source':source['filename']}
        if not evidence.exists():
            row.update(status='SKIPPED',reason='EVIDENCE_NOT_FOUND'); skipped+=1; cases.append(row); continue
        # Currently field runner registry contains the APF periodic-audio case. More runners can be added without changing manifests.
        if 'periodic_interference_count_min' not in (m.get('expected') or {}):
            row.update(status='SKIPPED',reason='NO_REGISTERED_FIELD_RUNNER'); skipped+=1; cases.append(row); continue
        with tempfile.TemporaryDirectory() as td:
            result=Path(td)/'result.json'; art=Path(td)/'artifacts'
            cmd=[sys.executable,str(ROOT/'tools/golden_audio_replay.py'),str(evidence),'--manifest',str(mp),'--out-dir',str(art),'--result',str(result)]
            cp=subprocess.run(cmd,cwd=ROOT,text=True,capture_output=True)
            payload=json.loads(result.read_text()) if result.exists() else {'error':cp.stderr[-2000:]}
            ok=cp.returncode==0 and payload.get('passed') is True
            row.update(status='PASSED' if ok else 'FAILED',checks_passed=sum(1 for c in payload.get('checks',[]) if c.get('passed')),checks_total=len(payload.get('checks',[])),analysis_status=payload.get('analysis_status'))
            if not ok: row['details']=payload; failed+=1
        cases.append(row)
    overall=failed==0 and (not args.require_all or skipped==0)
    out=evidence_envelope(evidence_type='FIELD_GOLDEN', payload={'passed':overall,'failed':failed,'skipped':skipped,'case_count':len(cases),'cases':cases}); args.out.parent.mkdir(parents=True, exist_ok=True); args.out.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n', encoding='utf-8'); print(json.dumps({k:out[k] for k in ('passed','failed','skipped','case_count','source_manifest_aggregate_sha256')},ensure_ascii=False,indent=2)); return 0 if overall else 1
if __name__=='__main__': raise SystemExit(main())
