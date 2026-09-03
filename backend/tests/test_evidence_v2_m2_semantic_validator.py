from app.reports.v2.correlation import absorb_member_findings, correlate_media_events
from app.reports.v2.finding_events import aggregate_events, build_event
from app.reports.v2.semantic_validator import validate_m2_semantics
from app.reports.v2.visibility import calculate_visibility


# This suite intentionally exercises the production M2 contract on the exact PR
# head; a prior full-acceptance attempt was runner-killed with exit 137.
def _base():
    return (
        {"call_end_time": None, "termination": {"observed": False}},
        {"media_observation_window": {"start": 10.0, "end": 20.0, "source": "RTP_OBSERVATION"}},
        [{"packet_count": 100}],
    )


def _rules(result):
    return {item["rule"] for item in result["violations"]}


def test_m2_validator_accepts_deduplicated_timing_cluster_and_partial_visibility():
    call, timeline, rtp = _base()
    rx = build_event(event_id="rx", observation_type="PCM_PACKET_INTERVAL_SPIKE", timestamp=15.0, layer="PCM_RX", source_ref="pcm-rx", call_id="c1")
    up = build_event(event_id="up", observation_type="RTP_HIGH_DELTA", timestamp=15.002, layer="RTP_UPSTREAM", source_ref="rtp-up", call_id="c1")
    findings = [
        aggregate_events([rx], finding_id="f-rx", finding_type="PCM_PACKET_INTERVAL_SPIKE", severity="MEDIUM"),
        aggregate_events([up], finding_id="f-up", finding_type="RTP_HIGH_DELTA", severity="MEDIUM"),
        {"finding_id": "normal", "class": "NORMAL", "severity": "INFO", "event_refs": [], "events": []},
    ]
    clusters = correlate_media_events([rx, up])
    findings = absorb_member_findings(findings, clusters)
    visibility = calculate_visibility(
        media_legs=[
            {"role": "CALLER", "directions": ["UPSTREAM", "DOWNSTREAM"]},
            {"role": "CALLEE", "directions": ["DOWNSTREAM"]},
        ]
    )

    result = validate_m2_semantics(
        call=call,
        timeline=timeline,
        rtp_streams=rtp,
        findings=findings,
        clusters=clusters,
        reported_problem_count=1,
        visibility=visibility,
        claims={"end_to_end_media_complete": False},
    )

    assert result["status"] == "PASS"
    assert result["violations"] == []


def test_r004_rejects_normal_evidence_counted_as_problem():
    call, timeline, rtp = _base()
    findings = [
        {"finding_id": "a", "class": "ABNORMAL", "event_refs": [], "events": []},
        {"finding_id": "n", "class": "NORMAL", "severity": "INFO", "event_refs": [], "events": []},
    ]

    result = validate_m2_semantics(
        call=call,
        timeline=timeline,
        rtp_streams=rtp,
        findings=findings,
        reported_problem_count=2,
    )
    assert "R004" in _rules(result)


def test_r007_rejects_discrete_events_rendered_as_continuous():
    call, timeline, rtp = _base()
    e1 = build_event(event_id="1", observation_type="PACKET_INTERVAL_SPIKE", timestamp=12.0, layer="PCM_RX", source_ref="p")
    e2 = build_event(event_id="2", observation_type="PACKET_INTERVAL_SPIKE", timestamp=18.0, layer="PCM_RX", source_ref="p")
    finding = aggregate_events([e1, e2], finding_id="f", finding_type="PCM_PACKET_INTERVAL_SPIKE", severity="MEDIUM")
    finding["continuous"] = True

    result = validate_m2_semantics(call=call, timeline=timeline, rtp_streams=rtp, findings=[finding])
    assert "R007" in _rules(result)


def test_r008_rejects_end_to_end_overclaim():
    call, timeline, rtp = _base()
    visibility = calculate_visibility(
        media_legs=[
            {"role": "CALLER", "directions": ["UPSTREAM", "DOWNSTREAM"]},
            {"role": "CALLEE", "directions": ["DOWNSTREAM"]},
        ]
    )

    result = validate_m2_semantics(
        call=call,
        timeline=timeline,
        rtp_streams=rtp,
        visibility=visibility,
        claims={"end_to_end_media_complete": True},
    )
    assert "R008" in _rules(result)


def test_r009_and_r014_reject_timing_or_continuous_sequence_as_loss():
    call, timeline, rtp = _base()
    timing = build_event(event_id="t", observation_type="RTP_HIGH_DELTA", timestamp=15.0, layer="RTP_UPSTREAM", source_ref="rtp")
    finding = aggregate_events([timing], finding_id="loss", finding_type="RTP_SEQUENCE_LOSS", severity="HIGH")
    finding["metrics"] = {"sequence_continuous": True, "lost_packets": 0}

    result = validate_m2_semantics(call=call, timeline=timeline, rtp_streams=rtp, findings=[finding])
    assert {"R009", "R014"}.issubset(_rules(result))


def test_r015_rejects_cluster_member_finding_not_absorbed():
    call, timeline, rtp = _base()
    rx = build_event(event_id="rx", observation_type="PACKET_INTERVAL_SPIKE", timestamp=15.0, layer="PCM_RX", source_ref="p", call_id="c")
    up = build_event(event_id="up", observation_type="RTP_HIGH_DELTA", timestamp=15.001, layer="RTP_UPSTREAM", source_ref="rtp", call_id="c")
    clusters = correlate_media_events([rx, up])
    finding = aggregate_events([rx], finding_id="f", finding_type="PCM_PACKET_INTERVAL_SPIKE", severity="MEDIUM")

    result = validate_m2_semantics(call=call, timeline=timeline, rtp_streams=rtp, findings=[finding], clusters=clusters)
    assert "R015" in _rules(result)
