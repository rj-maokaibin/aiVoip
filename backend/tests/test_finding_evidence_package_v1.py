from types import SimpleNamespace

from app.reports.evidence_brief import _attach_deterministic_summary_graphs, render_report_html
from app.reports.evidence_package import build_finding_evidence_package, build_report_evidence_packages
from app.services.evidence_report_source_artifacts import _artifact_matches_finding


def _artifact_ref(atype, aid, *, metadata=None, role="FINDING"):
    return {
        "artifact_id": aid,
        "type": atype,
        "filename": f"{aid}.dat",
        "content_type": "audio/wav" if "AUDIO" in atype else "image/png",
        "role": role,
        "sha256": "a" * 64,
        "metadata": metadata or {},
    }


def test_periodic_finding_package_assigns_primary_graph_and_three_audio_roles():
    finding = {
        "finding_id": "f-periodic",
        "type": "LOCAL_CAPTURE_PERIODIC_INTERFERENCE",
        "severity": "HIGH",
        "time_range": {"start": 10.0, "end": 14.0, "representative": 10.5},
        "scope": {"call_id": "call-1", "pcm_tap": "pcm_rx", "pcm_session_index": 1, "upstream_rtp_stream_id": "up", "downstream_rtp_stream_id": "down"},
        "metrics": {"strength": {"pcm_rx": 0.9, "upstream_rtp": 0.8, "downstream_rtp": 0.2}},
        "correlation": {
            "first_observable_boundary": {"status": "OBSERVED_BOUNDARY", "first_observable_layer": "PCM_RX"},
            "cross_layer_observation": {"type": "PERIODIC_INTERFERENCE_PATH"},
        },
        "interpretation": "周期干扰在 PCM_RX 已可观测。",
        "root_cause_boundary": "不能确认电源、接地、SLIC 等物理根因。",
        "artifact_refs": [
            _artifact_ref("SPECTRUM_PNG", "spectrum"),
            _artifact_ref("WAVEFORM_PNG", "wave"),
            _artifact_ref("PERIODIC_AUDIO_CLIP", "pcm", metadata={"source": "pcm_rx"}),
            _artifact_ref("PERIODIC_AUDIO_CLIP", "up", metadata={"source": "rtp_up"}),
            _artifact_ref("PERIODIC_AUDIO_CLIP", "down", metadata={"source": "rtp_down"}),
        ],
    }
    package = build_finding_evidence_package(finding, finding["artifact_refs"])
    assert package["reviewability"] == "FULLY_REVIEWABLE"
    assert package["primary_graph"]["artifact_id"] == "spectrum"
    assert package["primary_audio_clip"]["artifact_id"] == "pcm"
    assert [x["artifact_id"] for x in package["comparison_audio_clips"]] == ["up"]
    assert [x["artifact_id"] for x in package["control_audio_clips"]] == ["down"]
    assert package["first_observable_boundary"]["first_observable_layer"] == "PCM_RX"
    assert "A/B" in package["next_validation"]


def test_high_delta_package_requires_timeline_and_packet_refs_but_not_audio_clip():
    finding = {
        "finding_id": "f-delta",
        "type": "HIGH_DELTA",
        "severity": "MEDIUM",
        "time_range": {"start": 20.0, "end": 21.0, "representative": 20.2},
        "scope": {"rtp_stream_id": "rtp-up", "direction": "a->b"},
        "metrics": {
            "incident_count": 2,
            "packet_loss_relation": "NO_SEQUENCE_GAP_AND_STREAM_LOSS_ZERO",
            "incidents": [
                {"incident_id": "i1", "stream_id": "rtp-up", "delta_ms": 146.083, "expected_ptime_ms": 20.0, "stream_lost_packets": 0,
                 "call_relative_time_seconds": 37.1, "packet_refs": [{"role": "previous", "frame_number": 100}, {"role": "current", "frame_number": 101}]},
                {"incident_id": "i2", "stream_id": "rtp-up", "delta_ms": 175.043, "expected_ptime_ms": 20.0, "stream_lost_packets": 0,
                 "call_relative_time_seconds": 37.4, "packet_refs": [{"role": "previous", "frame_number": 120}, {"role": "current", "frame_number": 121}]},
            ],
        },
        "correlation": {},
        "interpretation": "节奏停顿，不等同丢包。",
        "root_cause_boundary": "不能确认具体网络或设备根因。",
        "artifact_refs": [_artifact_ref("RTP_TIMELINE_PNG", "timeline", role="SUMMARY_GRAPH")],
    }
    package = build_finding_evidence_package(finding, finding["artifact_refs"])
    assert package["reviewability"] == "FULLY_REVIEWABLE"
    assert package["primary_graph"]["artifact_id"] == "timeline"
    assert package["primary_audio_clip"] is None
    assert len(package["packet_refs"]) == 4
    assert package["key_metrics"]["delta_ms"] == [146.083, 175.043]


def test_audible_finding_without_clip_is_explicitly_partial_not_silently_complete():
    finding = {
        "finding_id": "f-click",
        "type": "CLICK_POP",
        "severity": "MEDIUM",
        "time_range": {"start": 1.0, "end": 1.0},
        "scope": {"pcm_tap": "pcm_rx", "pcm_session_index": 1},
        "metrics": {"candidate_id": "cand-1"},
        "correlation": {},
        "root_cause_boundary": "candidate only",
        "artifact_refs": [_artifact_ref("WAVEFORM_PNG", "wave")],
    }
    package = build_finding_evidence_package(finding, finding["artifact_refs"])
    assert package["reviewability"] == "PARTIALLY_REVIEWABLE"
    assert package["missing_required_evidence"] == ["PRIMARY_AUDIO_CLIP"]


def test_report_package_summary_is_written_back_to_each_finding():
    findings = [{
        "finding_id": "f1", "type": "HIGH_DELTA", "severity": "MEDIUM", "scope": {"rtp_stream_id": "s"},
        "time_range": {}, "metrics": {"incidents": [{"packet_refs": [{"frame_number": 1}], "stream_id": "s"}]},
        "correlation": {}, "artifact_refs": [_artifact_ref("RTP_TIMELINE_PNG", "timeline")],
    }]
    result = build_report_evidence_packages(findings)
    assert result["summary"]["FULLY_REVIEWABLE"] == 1
    assert findings[0]["evidence_package"]["finding_type"] == "HIGH_DELTA"


def test_summary_visual_fallback_is_explicit_and_deterministic():
    payload = {
        "artifacts": [_artifact_ref("RTP_TIMELINE_PNG", "timeline", role="SUMMARY")],
        "findings": [{"finding_id": "f", "type": "HIGH_DELTA", "artifact_refs": []}],
    }
    _attach_deterministic_summary_graphs(payload)
    ref = payload["findings"][0]["artifact_refs"][0]
    assert ref["artifact_id"] == "timeline"
    assert ref["role"] == "SUMMARY_GRAPH"
    assert ref["mapping_reason"] == "DETERMINISTIC_FINDING_TYPE_TO_REPORT_SUMMARY_VISUAL"


def test_event_clip_cannot_cross_attach_between_streams_at_same_time():
    artifact = SimpleNamespace(
        type="AUDIO_CLIP",
        metadata_json={"event_type": "HIGH_DELTA", "event_time": 10.0, "stream_id": "stream-a"},
    )
    correct = SimpleNamespace(
        finding_type="HIGH_DELTA", start_time=9.9, end_time=10.1,
        scope_json={"rtp_stream_id": "stream-a"},
    )
    wrong = SimpleNamespace(
        finding_type="HIGH_DELTA", start_time=9.9, end_time=10.1,
        scope_json={"rtp_stream_id": "stream-b"},
    )
    assert _artifact_matches_finding(artifact, correct) is True
    assert _artifact_matches_finding(artifact, wrong) is False


def test_pcm_silence_clip_normalizes_event_type_and_requires_same_session():
    artifact = SimpleNamespace(
        type="AUDIO_CLIP",
        metadata_json={"event_type": "SILENCE", "event_time": 10.5, "pcm_tap": "pcm_tx", "session_index": 2},
    )
    correct = SimpleNamespace(
        finding_type="UNEXPECTED_SILENCE", start_time=10.4, end_time=11.0,
        scope_json={"pcm_tap": "pcm_tx", "pcm_session_index": 2},
    )
    wrong = SimpleNamespace(
        finding_type="UNEXPECTED_SILENCE", start_time=10.4, end_time=11.0,
        scope_json={"pcm_tap": "pcm_tx", "pcm_session_index": 1},
    )
    assert _artifact_matches_finding(artifact, correct) is True
    assert _artifact_matches_finding(artifact, wrong) is False
