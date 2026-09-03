import json
from pathlib import Path

from app.reports.v2.correlation import (
    absorb_member_findings,
    correlate_media_events,
    correlation_problem_count,
)
from app.reports.v2.finding_events import aggregate_events, build_event
from app.reports.v2.semantic_validator import validate_m2_semantics
from app.reports.v2.visibility import calculate_visibility


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "preliminary_evidence" / "golden_002"


def _load(name: str):
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def test_golden_002_cross_layer_timing_cluster_is_one_problem_not_loss():
    foundation = _load("input.json")
    fixture = _load("events_m2.json")
    expected = fixture["expected"]

    events = [
        build_event(
            event_id=item["event_id"],
            observation_type=item["observation_type"],
            timestamp=item["timestamp"],
            layer=item["layer"],
            source_ref=item["source_ref"],
            call_id=fixture["call_id"],
            direction=item.get("direction"),
            metrics=item.get("metrics"),
            evidence_refs=item.get("evidence_refs"),
        )
        for item in fixture["events"]
    ]

    findings = [
        aggregate_events(
            [event],
            finding_id=f"F-{event['event_id']}",
            finding_type=event["observation_type"],
            severity="MEDIUM",
        )
        for event in events
    ]
    findings.extend(fixture["normal_evidence"])

    clusters = correlate_media_events(events, threshold_ms=50.0)
    findings = absorb_member_findings(findings, clusters)

    assert len(clusters) == expected["cluster_count"]
    cluster = clusters[0]
    assert cluster["cluster_id"] == expected["cluster_id"]
    assert cluster["type"] == expected["cluster_type"]
    assert cluster["packet_loss_observed"] is expected["packet_loss_observed"]
    assert cluster["interpretation_boundary"] == expected["interpretation_boundary"]
    assert [item["event_ref"] for item in cluster["member_events"]] == expected["member_event_refs"]
    assert correlation_problem_count(findings, clusters) == expected["problem_count"]

    rtp_upstream_event = next(event for event in events if event["layer"] == "RTP_UPSTREAM")
    assert rtp_upstream_event["event_family"] == "TIMING"
    assert rtp_upstream_event["metrics"]["sequence_continuous"] is True
    assert rtp_upstream_event["metrics"]["lost_packets"] == 0

    visibility = calculate_visibility(
        media_legs=[
            {"role": "CALLER", "directions": ["UPSTREAM", "DOWNSTREAM"]},
            {"role": "CALLEE", "directions": ["DOWNSTREAM"]},
        ],
        termination={"observed": False},
    )
    assert visibility["media"]["end_to_end"] == expected["end_to_end_media"]

    call = {"call_end_time": None, "termination": {"observed": False}}
    timeline = {
        "media_observation_window": {
            "start": min(item["start_time"] for item in foundation["rtp_streams"]),
            "end": max(item["end_time"] for item in foundation["rtp_streams"]),
            "source": "RTP_OBSERVATION",
        }
    }
    validation = validate_m2_semantics(
        call=call,
        timeline=timeline,
        rtp_streams=foundation["rtp_streams"],
        findings=findings,
        clusters=clusters,
        reported_problem_count=expected["problem_count"],
        visibility=visibility,
        claims={"end_to_end_media_complete": False},
    )
    assert validation["status"] == "PASS", validation


def test_golden_002_normal_dtmf_match_is_not_a_problem():
    fixture = _load("events_m2.json")
    normal = fixture["normal_evidence"][0]

    assert normal["class"] == "NORMAL"
    assert normal["severity"] == "INFO"
    assert normal["pcm_digits"] == "101"
    assert normal["sip_target"] == "101"
