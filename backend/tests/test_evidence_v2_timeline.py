from app.reports.v2.timeline import build_timeline_v2, event_relative_time


def test_media_window_is_derived_from_observed_rtp_not_ack():
    call = {
        "invite_time": 10.0,
        "established_time": 12.0,
        "capture_last_signaling_time": 12.0,
        "call_end_time": None,
        "termination": {"observed": False, "kind": None, "time": None},
    }
    streams = [
        {"stream_id": "a", "packet_count": 10, "start_time": 12.02, "end_time": 20.0},
        {"stream_id": "b", "packet_count": 10, "start_time": 12.03, "end_time": 19.98},
    ]

    timeline = build_timeline_v2(call, streams)
    media = timeline["media_observation_window"]

    assert media["start"] == 12.02
    assert media["end"] == 20.0
    assert media["duration_seconds"] == 7.98
    assert media["source"] == "RTP_OBSERVATION"
    assert media["start"] != call["established_time"]
    assert timeline["call_end_time"] is None


def test_empty_rtp_does_not_fabricate_media_window_from_signaling():
    call = {
        "invite_time": 10.0,
        "established_time": 12.0,
        "capture_last_signaling_time": 12.0,
        "call_end_time": None,
        "termination": {"observed": False},
    }

    timeline = build_timeline_v2(call, [])

    assert timeline["media_observation_window"] == {
        "start": None,
        "end": None,
        "duration_seconds": None,
        "source": "UNAVAILABLE",
        "stream_count": 0,
    }


def test_relative_time_uses_explicit_anchor():
    assert event_relative_time(20.125, 18.0) == 2.125
    assert event_relative_time(None, 18.0) is None
