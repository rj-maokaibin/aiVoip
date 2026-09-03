from app.reports.v2.composer import compose_preliminary_report_v2
from app.reports.v2.correlation import absorb_member_findings
from app.reports.v2.finding_events import aggregate_events, build_event


def test_v2_composer_first_page_is_decision_oriented_and_validator_passes():
    rx = build_event(event_id="rx", observation_type="PACKET_INTERVAL_SPIKE", timestamp=3.0,
                     layer="PCM_RX", source_ref="E-RX", call_id="call-1", evidence_refs=["E-RX"])
    rtp = build_event(event_id="rtp", observation_type="PACKET_INTERVAL_SPIKE", timestamp=3.001,
                      layer="RTP_UPSTREAM", source_ref="E-RTP", call_id="call-1", evidence_refs=["E-RTP"])
    cluster = {
        "cluster_id": "CC-001", "type": "CROSS_LAYER_MEDIA_TIMING_SPIKE", "representative_time": 3.0,
        "member_events": [{"layer": "PCM_RX", "event_ref": "rx"}, {"layer": "RTP_UPSTREAM", "event_ref": "rtp"}],
        "interpretation_boundary": "TIMING_CORRELATION_ONLY", "packet_loss_observed": False,
    }
    findings = absorb_member_findings([
        aggregate_events([rx], finding_id="F-RX", finding_type="PCM_PACKET_INTERVAL_SPIKE", severity="MEDIUM"),
        aggregate_events([rtp], finding_id="F-RTP", finding_type="RTP_HIGH_DELTA", severity="MEDIUM"),
    ], [cluster])
    for finding in findings:
        finding["evidence_refs"] = ["E-RX"] if finding["finding_id"] == "F-RX" else ["E-RTP"]

    report = compose_preliminary_report_v2(
        report_id="R1",
        call_reconstruction={"call_id": "call-1", "invite_time": 1.0, "established_time": 2.0,
                             "call_end_time": None, "termination": {"observed": False}},
        timeline={"media_observation_window": {"start": 2.1, "end": 5.0, "source": "RTP_OBSERVATION"}},
        rtp_streams=[{"packet_count": 100}], events=[rx, rtp], findings=findings,
        correlation_clusters=[cluster],
        visibility={"end_to_end_media": "PARTIAL", "termination": "NOT_OBSERVED",
                    "root_cause_readiness": "INSUFFICIENT"},
        normal_evidence=[{"type": "DTMF_SIP_DIAL_MATCH", "pcm": "101", "sip": "101"}],
        symptom_assessment={"reproduced": False, "detail": "PCM DTMF 101 与 SIP target 101 一致。"},
    )

    assert report["schema"] == "preliminary-evidence-report-v2"
    assert report["problem_count"] == 1
    assert report["semantic_validation"]["status"] == "PASS"
    assert report["publishable"] is True
    assert report["first_page"]["symptom_reproduction"] == "本次未复现"
    assert "timing spike" in report["first_page"]["conclusion"]
    assert report["first_page"]["next_steps"]


def test_validation_failure_never_remains_complete_or_publishable():
    report = compose_preliminary_report_v2(
        report_id="R2",
        call_reconstruction={"invite_time": 1.0, "established_time": 2.0, "call_end_time": 2.0,
                             "termination": {"observed": False}},
        timeline={"media_observation_window": {"start": 2.1, "end": 4.0, "source": "RTP_OBSERVATION"}},
        rtp_streams=[{"packet_count": 10}], events=[], findings=[], correlation_clusters=[],
        visibility={"end_to_end_media": "PARTIAL", "termination": "NOT_OBSERVED",
                    "root_cause_readiness": "INSUFFICIENT"},
    )
    assert report["semantic_validation"]["status"] == "FAIL"
    assert report["pipeline_status"] == "FAILED_VALIDATION"
    assert report["publishable"] is False
