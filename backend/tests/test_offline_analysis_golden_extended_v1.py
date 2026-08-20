from __future__ import annotations

from pathlib import Path

import yaml

from app.golden.offline_analysis_extended_checks import validate_extended_offline_truth


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "golden_cases" / "OFFLINE_ANALYSIS_20260814_001" / "manifest.yaml"
CALL_ID = "00ad1c804c33b255@192.168.3.200"


def _manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def _bundle() -> dict:
    high_delta = [
        {"type": "HIGH_DELTA", "details": {"delta_ms": 146.083, "previous_frame_number": 20272, "current_frame_number": 20285, "previous_sequence": 46511, "current_sequence": 46512}},
        {"type": "HIGH_DELTA", "details": {"delta_ms": 175.043, "previous_frame_number": 20329, "current_frame_number": 20344, "previous_sequence": 46519, "current_sequence": 46520}},
    ]
    call = {"id": "CALL-001", "sip_call_id": CALL_ID, "dialed_number": "601"}
    context = {"analysis_mode": "OFFLINE_IMPORTED", "call_origin": "RECONSTRUCTED_FROM_PCAP", "call_scope": "BOUND", "semantic_status": "OK", "reviewability": "FULLY_REVIEWABLE"}
    dtmf = {"type": "DTMF_SIP_DIAL_MATCH", "scope": {"call_id": CALL_ID, "pcm_tap": "pcm_rx"}, "details": {"call_id": CALL_ID, "pcm_digits": "601", "sip_target": "601"}}
    return {
        "pcm": {
            "summary": {"total_packets": 13050},
            "format": {"sample_rate": 8000, "bit_depth": 16, "endian": "little", "udp_payload_bytes": 160},
            "streams": [
                {"tap": {"name": "pcm_rx", "direction": "RX"}, "packet_count": 6525,
                 "source_endpoints": [{"ip": "192.168.150.4", "port": 48741, "packet_count": 6525}], "sessions": []},
                {"tap": {"name": "pcm_tx", "direction": "TX"}, "packet_count": 6525,
                 "source_endpoints": [{"ip": "192.168.150.4", "port": 46812, "packet_count": 6525}], "sessions": []},
            ],
        },
        "packet": {
            "rtp_streams": [{
                "src_ip": "192.168.150.4", "src_port": 10000,
                "dst_ip": "192.168.3.200", "dst_port": 11446,
                "events": high_delta,
            }]
        },
        "media": {"cross_layer_events": [dtmf]},
        "analysis_context": context,
        "display_call": call,
        "report": {
            "analysis_context": context,
            "display_call": call,
            "findings": [
                {"type": "HIGH_DELTA"},
                {
                    "type": "LOCAL_CAPTURE_PERIODIC_INTERFERENCE",
                    "root_cause_boundary": "当前仅确认周期/工频族特征，不能确认电源、接地、话柄、线路或 SLIC 为物理根因，需进一步受控验证。",
                },
                {"type": "DTMF_SIP_DIAL_MATCH"},
            ],
        },
    }


def _failed(bundle: dict) -> set[str]:
    return {x.name for x in validate_extended_offline_truth(bundle, _manifest()) if not x.passed}


def test_extended_pcm_and_frame_truth_passes():
    checks = validate_extended_offline_truth(_bundle(), _manifest())
    assert checks
    assert all(x.passed for x in checks), [(x.name, x.actual, x.expected) for x in checks if not x.passed]


def test_pcm_packet_count_regression_is_blocked():
    bundle = _bundle()
    bundle["pcm"]["streams"][0]["packet_count"] = 6524
    assert "pcm.tap.pcm_rx.packet_count" in _failed(bundle)


def test_pcm_format_regression_is_blocked():
    bundle = _bundle()
    bundle["pcm"]["format"]["sample_rate"] = 16000
    assert "pcm.format.sample_rate" in _failed(bundle)


def test_pcm_source_device_identity_regression_is_blocked():
    bundle = _bundle()
    bundle["pcm"]["streams"][1]["source_endpoints"][0]["ip"] = "192.168.150.99"
    assert "pcm.source_device_ip" in _failed(bundle)


def test_high_delta_frame_or_sequence_drift_is_blocked():
    bundle = _bundle()
    bundle["packet"]["rtp_streams"][0]["events"][0]["details"]["current_sequence"] = 46513
    failed = _failed(bundle)
    assert "rtp.high_delta.1.current_sequence" in failed
    assert "rtp.high_delta.1.sequence_continuity" in failed


def test_b2bua_duplicate_dtmf_match_is_blocked():
    bundle = _bundle()
    duplicate = {"type": "DTMF_SIP_DIAL_MATCH", "scope": {"call_id": "pbx-leg"}, "details": {"call_id": "pbx-leg", "pcm_digits": "601", "sip_target": "601"}}
    bundle["media"]["cross_layer_events"].append(duplicate)
    assert "dtmf.match_count" in _failed(bundle)


def test_dtmf_must_bind_to_subject_call():
    bundle = _bundle()
    bundle["media"]["cross_layer_events"][0]["details"]["call_id"] = "pbx-leg"
    bundle["media"]["cross_layer_events"][0]["scope"]["call_id"] = "pbx-leg"
    assert "dtmf.subject_call_id" in _failed(bundle)


def test_required_report_finding_disappearing_is_blocked():
    bundle = _bundle()
    bundle["report"]["findings"] = [x for x in bundle["report"]["findings"] if x["type"] != "DTMF_SIP_DIAL_MATCH"]
    assert "report.required_finding.DTMF_SIP_DIAL_MATCH" in _failed(bundle)


def test_report_context_must_match_replay_context():
    bundle = _bundle()
    bundle["report"]["analysis_context"] = {**bundle["analysis_context"], "analysis_mode": "REPRODUCTION"}
    assert "report.analysis_context_consistency" in _failed(bundle)
