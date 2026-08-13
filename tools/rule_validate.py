#!/usr/bin/env python3
from pathlib import Path
import argparse
from app.rules.compiler import load_rule_yaml

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('root',nargs='?',default='rules/diagnosis'); args=ap.parse_args()
    root=Path(args.root); failed=0
    for p in sorted(root.glob('*.yaml')):
        try:
            r=load_rule_yaml(p.read_text(encoding='utf-8')); print(f'OK {r.key}@{r.version} {r.checksum[:12]} {p}')
        except Exception as exc:
            failed+=1; print(f'FAIL {p}: {exc}')
    raise SystemExit(1 if failed else 0)
if __name__=='__main__': main()
