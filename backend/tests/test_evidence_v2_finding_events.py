from app.reports.v2.finding_events import (
    aggregate_events,
    build_event,
    observation_family,
    problem_count,
)


def test_timing_observation_is_not_loss():
    assert observation_family("PCM_PACKET_INTERVAL_SPIKE") == "TIMING"
    assert observation_family("RTP_HIGH_DELTA") == "TIMING"
    assert observation_family("RTP_SEQUENCE_LOSS") == "LOSS"


def test_multiple_instant_events_remain_discrete():
    events = [
        build_event(
            event_id="e1",
            observation_type="PCM_PACKET_INTERVAL_SPIKE",
            timestamp=10.0,
            layer="PCM_RX",
            source_ref="pcm-rx",
        ),
        build_event(
            event_id="e2",
            observation_type="PCM_PACKET_INTERVAL_SPIKE",
            timestamp=20.0,
            layer="PCM_RX",
            source_ref="pcm-rx",
        ),
    ]

    finding = aggregate_events(
        events,
        finding_id="f1",
        finding_type="PCM_PACKET_INTERVAL_SPIKE",
        severity="MEDIUM",
    )

    assert finding["class"] == "ABNORMAL"
    assert finding["event_count"] == 2
    assert finding["time_span"] == {"start": 10.0, "end": 20.0}
    assert finding["continuous"] is False
    assert finding["event_refs"] == ["e1", "e2"]


def test_problem_count_excludes_non_abnormal_and_absorbed_findings():
    findings = [
        {"finding_id": "a", "class": "ABNORMAL", "absorbed_by_cluster": None},
        {"finding_id": "b", "class": "NORMAL", "absorbed_by_cluster": None},
        {"finding_id": "c", "class": "EXCLUSION", "absorbed_by_cluster": None},
        {"finding_id": "u", "class": "UNCERTAIN", "absorbed_by_cluster": None},
        {"finding_id": "q", "class": "EVIDENCE_QUALITY", "absorbed_by_cluster": None},
        {"finding_id": "d", "class": "ABNORMAL", "absorbed_by_cluster": "XLY-001"},
    ]

    assert problem_count(findings) == 1
