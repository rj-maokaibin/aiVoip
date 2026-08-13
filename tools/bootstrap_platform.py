#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, time
import httpx


def main() -> int:
    ap=argparse.ArgumentParser(description='Bootstrap reviewed Rule/Knowledge seed into a running VOIP AI platform.')
    ap.add_argument('--api',default='http://localhost:8000')
    ap.add_argument('--actor',default='system')
    ap.add_argument('--timeout',type=float,default=120)
    args=ap.parse_args(); base=args.api.rstrip('/')
    with httpx.Client(base_url=base,timeout=20) as c:
        deadline=time.monotonic()+args.timeout
        while True:
            try:
                r=c.get('/health/ready')
                if r.status_code==200: break
            except Exception: pass
            if time.monotonic()>=deadline: raise SystemExit('backend did not become ready')
            time.sleep(1)
        rr=c.post(f'/api/v1/rules/bootstrap?actor={args.actor}'); rr.raise_for_status()
        kr=c.post(f'/api/v1/knowledge/bootstrap?actor={args.actor}'); kr.raise_for_status()
        out={'rules':rr.json(),'knowledge':kr.json()}
        print(json.dumps(out,ensure_ascii=False,indent=2))
    return 0

if __name__=='__main__': raise SystemExit(main())
