from __future__ import annotations

import copy
from pathlib import Path

import yaml

from app.golden.offline_analysis_e2e import validate_offline_analysis_bundle


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "golden_cases" / "OFFLINE_ANALYSIS_20260814_001" / "manifest.yaml"


def _manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def _bundle() -> dict:
    sip_id = "00ad1c804c33b255@192.168.3.200"
    pbx_leg_id = "60d32450633aea2363e5b73e-1786691379761-0x1067e2b4-2875d8158357@192.168.3.200"
    uplink_id = "192.168.150.4:10000>192.168.3.200:11446/ssrc=1"
    packet = {
        "status": "SUCCESS",
        "summary": {"call_count": 2, "rtp_stream_count": 3},
        "calls": [
            {
                "call_id": sip_id,
                "callee": "sip:601@192.168.3.200",
                "state": "TERMINATED",
                "media_direction_health": {"status": "BIDIRECTIONAL"},
                "rtp_stream_ids": [uplink_id, "reverse"],
            },
            {
                "call_id": pbx_leg_id,
                "callee": "sip:601@192.168.150.8",
                "state": "TERMINATED",
                "media_direction_health": {"status": "BIDIRECTIONAL"},
                "rtp_stream_ids": ["pbx-mirror"],
            },
        ],
        "rtp_streams": [{
            "stream_id": uplink_id,
            "src_ip": "192.168.150.4",
            "src_port": 10000,
            "dst_ip": "192.168.3.200",
            "dst_port": 11446,
            "codec": "PCMU",
            "lost_packets": 0,
            "events": [
                {"type": "HIGH_DELTA", "details": {"delta_ms": 146.083}},
                {"type": "HIGH_DELTA", "details": {"delta_ms": 175.043}},
            ],
        }],
        "anomalies": [{"type": "HIGH_DELTA"}, {"type": "HIGH_DELTA"}],
    }
    periodic = {
        "type": "LOCAL_CAPTURE_PERIODIC_INTERFERENCE",
        "time": 1786690970.0,
        "scope": {"pcm_tap": "pcm_rx", "pcm_session_index": 0, "upstream_rtp_stream_id": uplink_id, "downstream_rtp_stream_id": "reverse"},
        "details": {
            "strength": {"pcm_rx": 0.95, "upstream_rtp": 0.85, "downstream_rtp": 0.2},
            "pcm_rx": {"representative": {"autocorrelation": {"20ms": 0.91}}, "comb": {"hit_count": 8}},
            "upstream_rtp": {"representative": {"autocorrelation": {"20ms": 0.78}}},
            "downstream_rtp": {"representative": {"autocorrelation": {"20ms": 0.2}}},
        },
    }
    media = {
        "summary": {"candidate_decision": {"status_counts": {"REJECTED_NEGATIVE_CONTROL": 1}}},
        "cross_layer_events": [{"type": "DTMF_SIP_DIAL_MATCH", "details": {"pcm_digits": "601", "sip_target": "601", "pcm_tap": "pcm_rx"}}, periodic],
        "periodic_interference_paths": [periodic],
        "candidate_decisions": [
            {"candidate_type": "CLICK_POP", "candidate_time": 1786690964.332755, "status": "REJECTED_NEGATIVE_CONTROL", "reason_code": "DTMF_OVERLAP"},
            {"candidate_type": "UNEXPECTED_SILENCE", "candidate_time": 1786690990.0, "status": "REJECTED_NEGATIVE_CONTROL", "reason_code": "RTP_COUNTERPART_SILENCE"},
        ],
    }
    return {
        "source": {"filename": "tcpdump-2026-08-14(2).pcap", "sha256": "b038aa7c9a0644581f2815f654fcdee4620860796382265b178823fccba2e3f0"},
        "packet": packet,
        "media": media,
        "analysis_context": {
            "analysis_mode": "OFFLINE_IMPORTED",
            "call_origin": "RECONSTRUCTED_FROM_PCAP",
            "call_scope": "BOUND",
            "call_selection_status": "SELECTED",
            "selection_rule": "PCM_SOURCE_DEVICE_IDENTITY_MATCH",
            "subject_device_ip": "192.168.150.4",
            "semantic_status": "OK",
            "reviewability": "FULLY_REVIEWABLE",
        },
        "display_call": {"id": "CALL-001", "sip_call_id": sip_id, "dialed_number": "601"},
        "report": {
            "findings": [
                {"type": "HIGH_DELTA", "time_range": {"representative": 1786691020.0}},
                {"type": "LOCAL_CAPTURE_PERIODIC_INTERFERENCE", "time_range": {"representative": 1786690970.0}},
                {"type": "DTMF_SIP_DIAL_MATCH", "time_range": {"representative": 1786690965.0}},
            ]
        },
        "artifacts": [
            {"type": "PERIODIC_AUDIO_CLIP", "metadata": {"source": "pcm_rx"}},
            {"type": "PERIODIC_AUDIO_CLIP", "metadata": {"source": "rtp_up"}},
            {"type": "CANDIDATE_AUDIO_CLIP", "filename": "click.wav", "metadata": {"event_type": "CLICK_POP", "pcm_tap": "pcm_rx"}},
        ],
        "diagnosis": {"hypotheses": [{"code": "LOCAL_CAPTURE_PERIODIC_INTERFERENCE", "status": "SUPPORTED", "confidence": 0.96}]},
    }


def _failed_names(bundle: dict) -> set[str]:
    return {x.name for x in validate_offline_analysis_bundle(bundle, _manifest()) if not x.passed}


def test_offline_golden_baseline_truth_passes():
    checks = validate_offline_analysis_bundle(_bundle(), _manifest())
    assert checks
    assert all(x.passed for x in checks), [(x.name, x.actual, x.expected) for x in checks if not x.passed]


def test_packet_loss_is_a_blocking_regression():
    bundle = _bundle()
    bundle["packet"]["anomalies"].append({"type": "PACKET_LOSS"})
    assert "rtp.forbidden.PACKET_LOSS" in _failed_names(bundle)


def test_dtmf_onset_click_cannot_be_promoted_or_visible():
    bundle = _bundle()
    bundle["media"]["candidate_decisions"][0].update({"status": "PROMOTED", "reason_code": "ACTIVE_MEDIA_MULTI_FEATURE_CLICK"})
    bundle["report"]["findings"].append({"type": "CLICK_POP", "time_range": {"representative": 1786690964.332755}})
    failed = _failed_names(bundle)
    assert "candidate.required_negative_control" in failed
    assert "candidate.dtmf_click_not_promoted" in failed
    assert "report.dtmf_click_not_visible" in failed


def test_silence_requires_cross_layer_mismatch_reason_to_promote():
    bundle = _bundle()
    bundle["media"]["candidate_decisions"][1].update({"status": "PROMOTED", "reason_code": "ACTIVE_MEDIA_SILENCE"})
    assert "candidate.silence_promotion_grounded" in _failed_names(bundle)


def test_call_none_regression_is_blocked():
    bundle = _bundle()
    bundle["display_call"] = None
    failed = _failed_names(bundle)
    assert "call.diagnostic_call_count" in failed
    assert "report.display_call_required" in failed


def test_specific_hardware_root_confirmation_is_blocked():
    bundle = _bundle()
    bundle["diagnosis"]["hypotheses"].append({"code": "POWER_SUPPLY_NOISE", "status": "CONFIRMED", "confidence": 0.99})
    assert "diagnosis.no_specific_hardware_confirmation" in _failed_names(bundle)


def test_raw_candidate_audio_clip_cannot_masquerade_as_abnormal_audio():
    bundle = _bundle()
    bundle["artifacts"][2]["type"] = "AUDIO_CLIP"
    assert "artifacts.candidate_audio_quarantine" in _failed_names(bundle)


def test_truth_manifest_is_not_mutated_by_validation():
    manifest = _manifest()
    before = copy.deepcopy(manifest)
    validate_offline_analysis_bundle(_bundle(), manifest)
    assert manifest == before
