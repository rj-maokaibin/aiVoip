import json
from pathlib import Path

from app.reports.v2.call_reconstruction import reconstruct_call_v2
from app.reports.v2.timeline import build_timeline_v2


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "preliminary_evidence" / "golden_002"


def _load(name: str):
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def test_golden_002_call_and_media_semantics():
    source = _load("input.json")
    expected = _load("expected_v2.json")

    call = reconstruct_call_v2(source["sip_call"])
    timeline = build_timeline_v2(
        call,
        source["rtp_streams"],
        capture_window=source["capture_window"],
    )

    expected_call = expected["call"]
    assert call["state"] == expected_call["state"]
    assert call["invite_time"] == expected_call["invite_time"]
    assert call["answer_time"] == expected_call["answer_time"]
    assert call["ack_time"] == expected_call["ack_time"]
    assert call["established_time"] == expected_call["established_time"]
    assert call["call_end_time"] is expected_call["call_end_time"]
    assert call["termination"]["observed"] is expected_call["termination_observed"]

    expected_timeline = expected["timeline"]
    media = timeline["media_observation_window"]
    assert media["start"] == expected_timeline["media_start"]
    assert media["end"] == expected_timeline["media_end"]
    assert media["duration_seconds"] == expected_timeline["media_duration_seconds"]
    assert media["source"] == expected_timeline["media_source"]
    assert media["stream_count"] == expected_timeline["rtp_stream_count"]

    # Regression for the report that previously rendered ACK as both Call End
    # and a zero-length ACTIVE_MEDIA_WINDOW.
    assert call["call_end_time"] is None
    assert media["start"] != media["end"]
    assert media["start"] > call["established_time"]


def test_golden_002_source_identity_is_frozen():
    source = _load("input.json")["source"]
    assert source["sha256"] == "5d82ba014674797e8e9f8d3b86135064c5c84370dd322e7f1bceaf89214f5dfc"
    assert source["size_bytes"] == 1537456
