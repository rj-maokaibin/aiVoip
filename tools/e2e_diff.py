#!/usr/bin/env python3
from __future__ import annotations
import argparse,json
from pathlib import Path

def load(p): return json.loads(Path(p).read_text(encoding='utf-8'))
def index(x): return {c['id']:c for c in x.get('cases',[])}
def main():
    ap=argparse.ArgumentParser(description='Compare VOIP E2E observed baseline with a new replay')
    ap.add_argument('baseline'); ap.add_argument('actual'); ap.add_argument('--out',type=Path,default=Path('.e2e-diff.md')); args=ap.parse_args()
    b=index(load(args.baseline)); a=index(load(args.actual)); lines=['# VOIP E2E Regression Diff','']; regressions=0; changes=0
    for cid in sorted(set(b)|set(a)):
        if cid not in b: lines+= [f'## {cid}', '- NEW CASE']; changes+=1; continue
        if cid not in a: lines+= [f'## {cid}', '- MISSING CASE ❌']; regressions+=1; continue
        bc,ac=b[cid],a[cid]; diffs=[]
        if bc.get('passed') and not ac.get('passed'): diffs.append('PASS → FAIL ❌'); regressions+=1
        for key in ('anomaly_types','call_states','registration_states','media_direction_statuses','hypothesis_codes','matched_rules','plan_actions','conclusion_state'):
            bv=(bc.get('observed') or {}).get(key); av=(ac.get('observed') or {}).get(key)
            if bv!=av: diffs.append(f'`{key}`: `{bv}` → `{av}`'); changes+=1
        if diffs: lines += [f'## {cid}', *[f'- {d}' for d in diffs], '']
    if len(lines)==2: lines += ['No observed changes.']
    lines += ['',f'**Regressions:** {regressions}',f'**Observed changes:** {changes}']
    args.out.write_text('\n'.join(lines),encoding='utf-8'); print(json.dumps({'regressions':regressions,'changes':changes,'report':str(args.out)},ensure_ascii=False)); return 1 if regressions else 0
if __name__=='__main__': raise SystemExit(main())
