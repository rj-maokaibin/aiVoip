from __future__ import annotations

from app.reports.finding_composer import compose_findings


def _event(stream_id: str, *, when: float, delta: float, prev_frame: int, frame: int,
           prev_seq: int, seq: int, call_id: str, direction_role: str) -> dict:
    return {
        "type": "HIGH_DELTA",
        "severity": "MEDIUM",
        "time": when,
        "evidence": {
            "stream_id": stream_id,
            "call_id": call_id,
            "call_direction_role": direction_role,
            "delta_ms": delta,
            "expected_ptime_ms": 20.0,
            "threshold_ms": 60.0,
            "excess_delay_ms": delta - 20.0,
            "previous_frame_number": prev_frame,
            "current_frame_number": frame,
            "previous_sequence": prev_seq,
            "current_sequence": seq,
            "sequence_step": 1,
            "sequence_continuous": True,
            "sequence_gap_packets": 0,
            "classification": "INTERARRIVAL_STALL_WITHOUT_RTP_GAP",
            "loss_semantics": "NO_SEQUENCE_LOSS_AT_EVENT_BOUNDARY",
            "rtp_timestamp_step": 160,
            "expected_rtp_timestamp_step": 160,
            "rtp_timestamp_continuous": True,
            "catch_up": {
                "status": "PARTIAL",
                "observed": True,
                "recovered_delay_ms": 30.0,
                "recovery_ratio": 0.25,
            },
        },
    }


def _stream(stream_id: str, src_port: int, dst_port: int, *, call_id: str, role: str) -> dict:
    return {
        "stream_id": stream_id,
        "src_ip": "192.168.150.4" if src_port == 10000 else "192.168.3.200",
        "src_port": src_port,
        "dst_ip": "192.168.3.200" if dst_port == 11446 else "192.168.150.8",
        "dst_port": dst_port,
        "ssrc": 123,
        "packet_count": 2423,
        "lost_packets": 0,
        "loss_rate": 0.0,
        "p95_rfc3550_jitter_ms": 1.2,
        "max_delta_ms": 175.043,
        "high_delta_count": 2,
        "high_delta_without_sequence_loss_count": 2,
        "high_delta_catch_up_count": 2,
        "codec": "PCMU",
        "ptime_ms": 20.0,
        "primary_call_id": call_id,
        "call_direction_role": role,
    }


def test_high_delta_findings_are_aggregated_per_stream_not_across_mirrored_streams():
    dut_stream = "192.168.150.4:10000>192.168.3.200:11446/ssrc=123"
    mirrored_stream = "192.168.3.200:11452>192.168.150.8:10000/ssrc=123"
    packet = {
        "rtp_streams": [
            _stream(dut_stream, 10000, 11446, call_id="dut-call", role="OFFERER_TO_ANSWERER"),
            _stream(mirrored_stream, 11452, 10000, call_id="pbx-leg", role="OFFERER_TO_ANSWERER"),
        ],
        "anomalies": [
            _event(dut_stream, when=100.1, delta=146.083, prev_frame=20272, frame=20285,
                   prev_seq=46511, seq=46512, call_id="dut-call", direction_role="OFFERER_TO_ANSWERER"),
            _event(dut_stream, when=100.3, delta=175.043, prev_frame=20329, frame=20344,
                   prev_seq=46519, seq=46520, call_id="dut-call", direction_role="OFFERER_TO_ANSWERER"),
            _event(mirrored_stream, when=100.1, delta=146.0, prev_frame=20273, frame=20286,
                   prev_seq=900, seq=901, call_id="pbx-leg", direction_role="OFFERER_TO_ANSWERER"),
        ],
    }

    findings = [f for f in compose_findings(packet=packet, pcm=None, media=None, source_run_ids={"packet_intelligence": "run-1"}) if f["type"] == "HIGH_DELTA"]

    assert len(findings) == 2
    dut = next(f for f in findings if f["scope"]["rtp_stream_id"] == dut_stream)
    mirror = next(f for f in findings if f["scope"]["rtp_stream_id"] == mirrored_stream)

    assert dut["occurrence_count"] == 2
    assert dut["metrics"]["event_count"] == 2
    assert dut["metrics"]["max_delta_ms"] == 175.043
    assert dut["metrics"]["all_sequence_continuous"] is True
    assert len(dut["metrics"]["events"]) == 2
    assert dut["semantic_summary"]["event_family"] == "HIGH_DELTA"
    assert dut["semantic_summary"]["event_count"] == 2
    assert dut["semantic_summary"]["loss_interpretation"] == "DELAY_NOT_PACKET_LOSS"
    assert dut["semantic_summary"]["all_sequence_continuous"] is True
    assert dut["semantic_summary"]["catch_up_observed_count"] == 2
    assert "未观察到对应 RTP 丢包" in dut["observation"]
    assert "不应写成 Packet Loss" in dut["interpretation"]
    assert dut["scope"]["call_id"] == "dut-call"
    assert dut["scope"]["call_direction_role"] == "OFFERER_TO_ANSWERER"
    assert dut["scope"]["ssrc"] == 123

    assert mirror["occurrence_count"] == 1
    assert mirror["metrics"]["event_count"] == 1
    assert mirror["semantic_summary"]["loss_interpretation"] == "DELAY_NOT_PACKET_LOSS"
    assert mirror["scope"]["call_id"] == "pbx-leg"


def test_single_high_delta_finding_exposes_frame_seq_ptime_and_catch_up_semantics():
    stream_id = "10.0.0.1:10000>10.0.0.2:20000/ssrc=77"
    packet = {
        "rtp_streams": [{
            "stream_id": stream_id,
            "src_ip": "10.0.0.1",
            "src_port": 10000,
            "dst_ip": "10.0.0.2",
            "dst_port": 20000,
            "ssrc": 77,
            "packet_count": 100,
            "lost_packets": 0,
            "loss_rate": 0.0,
            "max_delta_ms": 146.0,
            "high_delta_count": 1,
            "high_delta_without_sequence_loss_count": 1,
            "high_delta_catch_up_count": 1,
            "codec": "PCMU",
            "ptime_ms": 20.0,
            "call_direction_role": "ANSWERER_TO_OFFERER",
        }],
        "anomalies": [
            _event(stream_id, when=10.146, delta=146.0, prev_frame=10, frame=20,
                   prev_seq=1000, seq=1001, call_id="call-a", direction_role="ANSWERER_TO_OFFERER")
        ],
    }

    finding = next(f for f in compose_findings(packet=packet, pcm=None, media=None) if f["type"] == "HIGH_DELTA")
    event = finding["metrics"]["events"][0]

    assert finding["title"] == "RTP 包间隔异常增大（HIGH_DELTA）"
    assert finding["semantic_summary"]["loss_interpretation"] == "DELAY_NOT_PACKET_LOSS"
    assert event["delta_ms"] == 146.0
    assert event["expected_ptime_ms"] == 20.0
    assert event["previous_frame_number"] == 10
    assert event["current_frame_number"] == 20
    assert event["previous_sequence"] == 1000
    assert event["current_sequence"] == 1001
    assert event["sequence_continuous"] is True
    assert event["catch_up"]["status"] == "PARTIAL"
    assert "单凭该事件也不能区分" in finding["root_cause_boundary"]
