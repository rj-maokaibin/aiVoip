#!/usr/bin/env python3
from __future__ import annotations

import argparse, json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'backend'))
from app.analyzers.pcm import PcmIntelligenceEngine, load_pcm_profile

p=argparse.ArgumentParser(description='Analyze private PCM UDP streams from a PCAP')
p.add_argument('pcap')
p.add_argument('--profile', default=str(ROOT/'profiles/pcm/ruijie_aim_diag_v1.yaml'))
p.add_argument('-o','--output')
a=p.parse_args()
result=PcmIntelligenceEngine(load_pcm_profile(a.profile)).analyze_pcap(a.pcap)
text=json.dumps(result,ensure_ascii=False,indent=2)
if a.output: Path(a.output).write_text(text,encoding='utf-8')
else: print(text)
