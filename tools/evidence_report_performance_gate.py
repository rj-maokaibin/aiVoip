#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.core.config import settings  # noqa: E402
from app.reports.finding_composer import compose_findings  # noqa: E402
from app.reports.evidence_visuals import (  # noqa: E402
    render_rtp_timeline_png, render_spectrogram_png, render_spectrum_png, render_waveform_png,
)
from app.reports.sip_flow_visual import render_sip_call_flow_png  # noqa: E402


def percentile(values: list[float], q: float) -> float:
    xs = sorted(values)
    idx = min(len(xs) - 1, max(0, int((len(xs) - 1) * q)))
    return xs[idx]


def synthetic_inputs(scale: int = 1) -> tuple[dict, dict, dict, dict, dict]:
    stream_count = max(2, 8 * scale)
    event_count = max(4, 16 * scale)
    rtp_streams=[]
    for i in range(stream_count):
        events=[]
        for j in range(event_count // 2):
            when=float(j * 2 + i) % 60
            events.append({
                "type":"HIGH_DELTA","start_time":when,"severity":"MEDIUM",
                "details":{"delta_ms":140.0+j,"previous_frame_number":1000+j,"current_frame_number":1001+j,
                           "previous_sequence":2000+j,"current_sequence":2001+j,"sequence_continuous":True},
            })
        rtp_streams.append({
            "stream_id":f"s{i}","src_ip":"10.0.0.1","src_port":10000+i,"dst_ip":"10.0.0.2","dst_port":20000+i,
            "packet_count":3000,"lost_packets":0,"loss_rate":0.0,"codec":"PCMU","ptime_ms":20,
            "start_time":0.0,"end_time":60.0,"ssrc":100+i,"events":events,
        })
    ladder=[]
    labels=["INVITE","100 Trying","180 Ringing","200 OK","ACK","BYE","200 OK"]
    for i,label in enumerate(labels):
        request=label in {"INVITE","ACK","BYE"}
        ladder.append({
            "frame_number":100+i,"timestamp":float(i),"src":"10.0.0.1:5060" if i % 2 == 0 else "10.0.0.2:5060",
            "dst":"10.0.0.2:5060" if i % 2 == 0 else "10.0.0.1:5060","label":label,
            "method":label if request else None,"status_code":None if request else int(label.split()[0]),
        })
    packet = {
        "rtp_streams":rtp_streams,
        "calls":[{"call_id":"synthetic-call","state":"TERMINATED","ladder":ladder}],
        "anomalies":[{
            "type":"HIGH_DELTA","severity":"MEDIUM","start_time":float(i),
            "evidence":{"stream_id":f"s{i % stream_count}","delta_ms":150.0,"expected_ptime_ms":20.0,
                        "previous_frame_number":3000+i,"current_frame_number":3001+i,
                        "previous_sequence":4000+i,"current_sequence":4001+i,"sequence_continuous":True},
        } for i in range(event_count)],
    }
    pcm = {
        "streams":[{"tap":{"name":"pcm_rx","direction":"RX"},"sessions":[{
            "session_index":0,"start_time":0.0,"end_time":60.0,
            "hum":{"level":"HIGH","dominant_family":"50Hz","score":0.9},
            "signal":{"rms_dbfs":-24.0,"peak_dbfs":-6.0},"gap_events":[],"silence_events":[],"click_pop_events":[],
            "spectral":{"peaks":[{"frequency_hz":50*(i+1),"energy_ratio":1.0/(i+1)} for i in range(12)]},
        }]}]
    }
    media={"cross_layer_events":[],"periodic_interference_paths":[]}
    waveform={"duration_seconds":60.0,"bins":[{"t":i*0.03,"min":-12000+(i%100),"max":12000-(i%100)} for i in range(2000)]}
    spec={"times":[i*0.05 for i in range(400)],"frequencies":[i*20 for i in range(128)],
          "db":[[-100+((i+j)%50) for j in range(128)] for i in range(400)]}
    return packet,pcm,media,waveform,spec


def run_once(scale: int) -> dict:
    packet,pcm,media,waveform,spec=synthetic_inputs(scale)
    t0=time.perf_counter()
    findings=compose_findings(packet=packet,pcm=pcm,media=media)
    t1=time.perf_counter()

    images=[
        render_rtp_timeline_png(packet["rtp_streams"],title="RTP TIMELINE - ALL STREAMS",subtitle="SYNTHETIC CALL"),
        render_sip_call_flow_png(packet["calls"],title="SIP CALL FLOW",subtitle="SYNTHETIC CALL"),
        render_spectrum_png(pcm["streams"][0]["sessions"][0]["spectral"],title="SPECTRUM PCM_RX",subtitle="SYNTHETIC"),
        render_waveform_png(waveform,anomaly_start=20.0,anomaly_end=21.0,title="WAVEFORM PERIODIC",subtitle="PCM_RX"),
        render_spectrogram_png(spec,anomaly_start=20.0,anomaly_end=21.0,title="SPECTROGRAM PERIODIC",subtitle="PCM_RX"),
    ]
    # Exercise the same bounded Finding-scoped rendering pattern introduced by
    # PR5. This is still a software-core benchmark, not a storage/Feishu/DUT SLA.
    for stream in packet["rtp_streams"][:min(8,len(packet["rtp_streams"]))]:
        images.append(render_rtp_timeline_png([stream],title="RTP HIGH_DELTA",subtitle=f"{stream['src_ip']}:{stream['src_port']}->{stream['dst_ip']}:{stream['dst_port']}"))
    for index in range(min(4,max(1,scale*2))):
        a=5.0+index
        images.append(render_waveform_png(waveform,anomaly_start=a,anomaly_end=a+0.5,title="WAVEFORM FINDING",subtitle="PCM_RX"))
        images.append(render_spectrogram_png(spec,anomaly_start=a,anomaly_end=a+0.5,title="SPECTROGRAM FINDING",subtitle="PCM_RX"))
    t2=time.perf_counter()
    return {
        "compose_seconds":t1-t0,"render_seconds":t2-t1,"total_seconds":t2-t0,
        "finding_count":len(findings),"rendered_image_count":len(images),"rendered_bytes":sum(len(x) for x in images),
    }


def main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--iterations",type=int,default=8)
    ap.add_argument("--scale",type=int,default=1)
    ap.add_argument("--out",type=Path,default=ROOT/"validation"/"evidence_report_performance_gate.json")
    args=ap.parse_args()
    rows=[run_once(args.scale) for _ in range(max(3,args.iterations))]
    totals=[r["total_seconds"] for r in rows]
    p50=percentile(totals,0.50);p95=percentile(totals,0.95);max_s=max(totals)
    software_budget=min(settings.evidence_report_basic_sla_seconds,10.0)
    passed=p95<=software_budget
    payload={
        "schema_version":"evidence-report-performance-gate-v2","status":"PASS" if passed else "FAIL",
        "iterations":len(rows),"scale":args.scale,"software_core_budget_seconds":software_budget,
        "workload_boundary":"Measures Finding composition plus PR5 production PNG renderer path with bounded focused visuals. Excludes DB/ObjectStorage/Feishu/network/DUT acquisition.",
        "metrics":{"p50_seconds":round(p50,4),"p95_seconds":round(p95,4),"max_seconds":round(max_s,4),"mean_seconds":round(statistics.mean(totals),4)},
        "rows":[{**r,"compose_seconds":round(r["compose_seconds"],4),"render_seconds":round(r["render_seconds"],4),"total_seconds":round(r["total_seconds"],4)} for r in rows],
        "production_sla":{
            "basic_result_seconds":settings.evidence_report_basic_sla_seconds,
            "full_report_p95_seconds":settings.evidence_report_full_p95_seconds,
            "large_call_p95_seconds":settings.evidence_report_large_p95_seconds,
            "real_dut_validation_required":True,
        },
    }
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"status":payload["status"],"metrics":payload["metrics"],"out":str(args.out)},ensure_ascii=False,indent=2))
    return 0 if passed else 2


if __name__=="__main__":
    raise SystemExit(main())
