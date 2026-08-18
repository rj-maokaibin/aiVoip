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
    render_rtp_timeline_png, render_sip_call_flow_png, render_spectrogram_png,
    render_spectrum_png, render_waveform_png,
)


def percentile(values: list[float], q: float) -> float:
    xs = sorted(values)
    idx = min(len(xs) - 1, max(0, int((len(xs) - 1) * q)))
    return xs[idx]


def synthetic_inputs(scale: int = 1) -> tuple[dict, dict, dict, dict, dict]:
    stream_count = max(2, 8 * scale)
    event_count = max(4, 16 * scale)
    packet = {
        "rtp_streams": [
            {"stream_id": f"s{i}", "src_ip": "10.0.0.1", "src_port": 10000 + i, "dst_ip": "10.0.0.2", "dst_port": 20000 + i,
             "packet_count": 3000, "lost_packets": 2, "loss_rate": 0.00066, "codec": "PCMU", "ptime_ms": 20,
             "start_time": 0.0, "end_time": 60.0,
             "events": [{"type": "HIGH_DELTA", "start_time": float(j * 2 + i) % 60, "severity": "MEDIUM"} for j in range(event_count // 2)]}
            for i in range(stream_count)
        ],
        "calls": [{"messages": [{"direction": "OUT" if i % 2 == 0 else "IN", "label": "INVITE"} for i in range(20)]}],
        "anomalies": [{"type": "HIGH_DELTA", "severity": "MEDIUM", "start_time": float(i), "evidence": {"stream_id": f"s{i % stream_count}"}} for i in range(event_count)],
    }
    pcm = {
        "streams": [{"tap": {"name": "pcm_rx", "direction": "RX"}, "sessions": [{
            "session_index": 0, "start_time": 0.0, "end_time": 60.0,
            "hum": {"level": "HIGH", "dominant_family": "50Hz", "score": 0.9},
            "signal": {"rms_dbfs": -24.0, "peak_dbfs": -6.0}, "gap_events": [], "silence_events": [], "click_pop_events": [],
            "spectral": {"peaks": [{"frequency_hz": 50 * (i + 1), "energy_ratio": 1.0 / (i + 1)} for i in range(12)]},
        }]}]
    }
    media = {"cross_layer_events": [], "periodic_interference_paths": []}
    waveform = {"duration_seconds": 60.0, "bins": [{"t": i * 0.03, "min": -12000 + (i % 100), "max": 12000 - (i % 100)} for i in range(2000)]}
    spec = {"times": [i * 0.05 for i in range(400)], "frequencies": [i * 20 for i in range(128)],
            "db": [[-100 + ((i + j) % 50) for j in range(128)] for i in range(400)]}
    return packet, pcm, media, waveform, spec


def run_once(scale: int) -> dict:
    packet, pcm, media, waveform, spec = synthetic_inputs(scale)
    t0 = time.perf_counter()
    findings = compose_findings(packet=packet, pcm=pcm, media=media)
    t1 = time.perf_counter()
    images = [
        render_waveform_png(waveform),
        render_spectrogram_png(spec),
        render_spectrum_png(pcm["streams"][0]["sessions"][0]["spectral"]),
        render_rtp_timeline_png(packet["rtp_streams"]),
        render_sip_call_flow_png(packet["calls"]),
    ]
    t2 = time.perf_counter()
    return {
        "compose_seconds": t1 - t0,
        "render_seconds": t2 - t1,
        "total_seconds": t2 - t0,
        "finding_count": len(findings),
        "rendered_bytes": sum(len(x) for x in images),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iterations", type=int, default=8)
    ap.add_argument("--scale", type=int, default=1)
    ap.add_argument("--out", type=Path, default=ROOT / "validation" / "evidence_report_performance_gate.json")
    args = ap.parse_args()
    rows = [run_once(args.scale) for _ in range(max(3, args.iterations))]
    totals = [r["total_seconds"] for r in rows]
    p50 = percentile(totals, 0.50)
    p95 = percentile(totals, 0.95)
    max_s = max(totals)
    # Software-core work must stay well inside the 10 s basic-result budget so
    # storage/queue overhead has headroom. Real Call End→Report P95 is separately
    # verified in the real-DUT environment and is not fabricated here.
    software_budget = min(settings.evidence_report_basic_sla_seconds, 10.0)
    passed = p95 <= software_budget
    payload = {
        "schema_version": "evidence-report-performance-gate-v1",
        "status": "PASS" if passed else "FAIL",
        "iterations": len(rows),
        "scale": args.scale,
        "software_core_budget_seconds": software_budget,
        "metrics": {
            "p50_seconds": round(p50, 4),
            "p95_seconds": round(p95, 4),
            "max_seconds": round(max_s, 4),
            "mean_seconds": round(statistics.mean(totals), 4),
        },
        "rows": [{**r, "compose_seconds": round(r["compose_seconds"], 4), "render_seconds": round(r["render_seconds"], 4), "total_seconds": round(r["total_seconds"], 4)} for r in rows],
        "production_sla": {
            "basic_result_seconds": settings.evidence_report_basic_sla_seconds,
            "full_report_p95_seconds": settings.evidence_report_full_p95_seconds,
            "large_call_p95_seconds": settings.evidence_report_large_p95_seconds,
            "real_dut_validation_required": True,
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": payload["status"], "metrics": payload["metrics"], "out": str(args.out)}, ensure_ascii=False, indent=2))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
