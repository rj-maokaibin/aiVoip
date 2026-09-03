from app.reports.v2.correlation import (
    absorb_member_findings,
    correlate_media_events,
    correlation_problem_count,
)
from app.reports.v2.finding_events import aggregate_events, build_event


def _event(event_id, layer, timestamp, call_id="call-1", media_path=None):
    return build_event(
        event_id=event_id,
        observation_type="PACKET_INTERVAL_SPIKE",
        timestamp=timestamp,
        layer=layer,
        source_ref=f"src-{event_id}",
        call_id=call_id,
        media_path=media_path,
    )


def test_same_call_timing_events_across_layers_form_one_candidate_cluster():
    events = [
        _event("pcm-rx", "PCM_RX", 100.000),
        _event("rtp-up", "RTP_UPSTREAM", 100.0011),
        _event("pcm-tx", "PCM_TX", 100.0020),
    ]

    clusters = correlate_media_events(events, threshold_ms=50.0)

    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster["cluster_id"] == "CC-001"
    assert cluster["type"] == "CROSS_LAYER_MEDIA_TIMING_SPIKE"
    assert cluster["member_layer_families"] == ["PCM", "RTP"]
    assert cluster["member_events"] == [
        {"layer": "PCM_RX", "event_ref": "pcm-rx"},
        {"layer": "RTP_UPSTREAM", "event_ref": "rtp-up"},
        {"layer": "PCM_TX", "event_ref": "pcm-tx"},
    ]
    assert cluster["packet_loss_observed"] is False
    assert cluster["interpretation_boundary"] == "TIMING_CORRELATION_ONLY"
    assert cluster["causality_confirmed"] is False
    assert cluster["root_cause_confirmed"] is False


def test_pcm_rx_and_tx_alone_are_not_cross_layer_media_cluster():
    events = [
        _event("rx", "PCM_RX", 100.0),
        _event("tx", "PCM_TX", 100.001),
    ]
    assert correlate_media_events(events) == []


def test_different_call_or_outside_window_does_not_cluster():
    events = [
        _event("a", "PCM_RX", 100.0, call_id="call-1"),
        _event("b", "RTP_UPSTREAM", 100.2, call_id="call-1"),
        _event("c", "PCM_TX", 100.001, call_id="call-2"),
    ]

    assert correlate_media_events(events, threshold_ms=50.0) == []


def test_correlation_window_is_inclusive_at_profile_boundary():
    events = [
        _event("a", "PCM_RX", 1.0),
        _event("b", "RTP_UPSTREAM", 1.05),
    ]

    clusters = correlate_media_events(events, threshold_ms=50.0)

    assert len(clusters) == 1
    assert clusters[0]["member_events"] == [
        {"layer": "PCM_RX", "event_ref": "a"},
        {"layer": "RTP_UPSTREAM", "event_ref": "b"},
    ]


def test_incompatible_explicit_media_paths_do_not_cluster():
    events = [
        _event("a", "PCM_RX", 100.0, media_path="caller-leg"),
        _event("b", "RTP_UPSTREAM", 100.001, media_path="callee-leg"),
    ]

    assert correlate_media_events(events, threshold_ms=50.0) == []


def test_member_findings_are_absorbed_and_problem_count_becomes_one_cluster():
    rx = _event("rx", "PCM_RX", 100.0)
    rtp = _event("rtp", "RTP_UPSTREAM", 100.001)
    findings = [
        aggregate_events(
            [rx],
            finding_id="f-rx",
            finding_type="PCM_PACKET_INTERVAL_SPIKE",
            severity="MEDIUM",
        ),
        aggregate_events(
            [rtp],
            finding_id="f-rtp",
            finding_type="RTP_HIGH_DELTA",
            severity="MEDIUM",
        ),
    ]
    clusters = correlate_media_events([rx, rtp])

    absorbed = absorb_member_findings(findings, clusters)
    assert {item["absorbed_by_cluster"] for item in absorbed} == {"CC-001"}
    assert correlation_problem_count(findings, clusters) == 1


def test_finding_with_clustered_and_unclustered_events_is_not_fully_absorbed():
    pre = _event("pre", "PCM_RX", 90.0)
    active = _event("active", "PCM_RX", 100.0)
    rtp = _event("rtp", "RTP_UPSTREAM", 100.001)
    finding = aggregate_events(
        [pre, active],
        finding_id="f-rx",
        finding_type="PCM_PACKET_INTERVAL_SPIKE",
        severity="MEDIUM",
    )
    clusters = correlate_media_events([pre, active, rtp])

    [normalized] = absorb_member_findings([finding], clusters)
    assert normalized["absorbed_by_cluster"] is None
    assert normalized["clustered_event_refs"] == ["active"]
    assert normalized["unclustered_event_refs"] == ["pre"]
    assert correlation_problem_count([finding], clusters) == 2
