from app.contracts.diagnostic import build_diagnostic_event


def test_plain_collection_index_does_not_change_diagnostic_event_identity():
    base = dict(
        event_type="HIGH_DELTA",
        analyzer_id="packet_intelligence",
        scope={"call_id": "call-1", "rtp_stream_id": "rtp-1", "ssrc": "0x1"},
        time_range={"start": 100.0, "end": 100.0, "representative": 100.0},
        measurements={"delta_ms": 175.0, "sequence_continuous": True},
    )
    first = build_diagnostic_event(**base, source_ref={"source": "packet.anomalies", "index": 0})
    reordered = build_diagnostic_event(**base, source_ref={"source": "packet.anomalies", "index": 9})
    assert first["event_id"] == reordered["event_id"]
    assert first["source_ref"] != reordered["source_ref"]


def test_stable_source_identity_still_separates_different_candidates():
    base = dict(
        event_type="CLICK_POP",
        analyzer_id="media_intelligence",
        scope={"call_id": "call-1", "pcm_tap": "pcm_rx"},
        time_range={"start": 10.0, "end": 10.0, "representative": 10.0},
        measurements={"confidence": 0.9},
    )
    a = build_diagnostic_event(**base, source_ref={"source": "media.candidate_decisions", "candidate_id": "candidate-a", "index": 0})
    b = build_diagnostic_event(**base, source_ref={"source": "media.candidate_decisions", "candidate_id": "candidate-b", "index": 0})
    assert a["event_id"] != b["event_id"]
