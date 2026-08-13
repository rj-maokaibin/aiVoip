#!/usr/bin/env python3
import argparse, json, sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]/'backend'
sys.path.insert(0,str(ROOT))
from app.analyzers.packet import PacketIntelligenceEngine


def main():
    ap=argparse.ArgumentParser(description='Analyze SIP/SDP/RTP from a PCAP/PCAPNG using TShark + VOIP semantic engine')
    ap.add_argument('pcap')
    ap.add_argument('-o','--output')
    args=ap.parse_args()
    result=PacketIntelligenceEngine().analyze_pcap(args.pcap)
    text=json.dumps(result,ensure_ascii=False,indent=2)
    if args.output: Path(args.output).write_text(text,encoding='utf-8')
    else: print(text)

if __name__=='__main__': main()
