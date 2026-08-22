from __future__ import annotations

from app.analyzers.packet.rtp import HIGH_DELTA_SEMANTICS_VERSION, RtpStreamAnalyzer
from app.analyzers.packet.types import NormalizedPacket, RtpData
from app.analyzers.profile import get_default_analyzer_profile


def _packet(frame: int, arrival: float, seq: int, rtp_ts: int, *, ssrc: int = 77) -> NormalizedPacket:
    return NormalizedPacket(
        frame_number=frame,
        timestamp=arrival,
        src_ip="10.0.0.10",
        dst_ip="10.0.0.20",
        transport="UDP",
        src_port=10000,
        dst_port=20000,
        protocols=["rtp"],
        rtp=RtpData(
            ssrc=ssrc,
            sequence=seq,
            timestamp=rtp_ts,
            payload_type=0,
            payload_size=160,
            payload_hex="ff" * 160,
        ),
    )


def _stream(packets: list[NormalizedPacket]) -> dict:
    result = RtpStreamAnalyzer().analyze(packets)
    assert len(result) == 1
    return result[0]


def test_high_delta_continuous_sequence_is_delay_not_packet_loss_and_records_catch_up():
    packets = [
        _packet(1, 0.000, 100, 0),
        _packet(2, 0.020, 101, 160),
        _packet(3, 0.040, 102, 320),
        _packet(4, 0.186, 103, 480),  # 146 ms inter-arrival stall
        _packet(5, 0.190, 104, 640),
        _packet(6, 0.194, 105, 800),
        _packet(7, 0.198, 106, 960),
        _packet(8, 0.202, 107, 1120),
        _packet(9, 0.206, 108, 1280),
        _packet(10, 0.210, 109, 1440),
        _packet(11, 0.214, 110, 1600),
        _packet(12, 0.218, 111, 1760),
    ]
    stream = _stream(packets)
    events = [event for event in stream["events"] if event["type"] == "HIGH_DELTA"]

    assert len(events) == 1
    details = events[0]["details"]
    assert details["semantic_version"] == HIGH_DELTA_SEMANTICS_VERSION
    assert details["classification"] == "INTERARRIVAL_STALL_WITHOUT_RTP_GAP"
    assert details["loss_semantics"] == "NO_SEQUENCE_LOSS_AT_EVENT_BOUNDARY"
    assert details["sequence_step"] == 1
    assert details["sequence_continuous"] is True
    assert details["sequence_gap_packets"] == 0
    assert details["previous_frame_number"] == 3
    assert details["current_frame_number"] == 4
    assert details["previous_sequence"] == 102
    assert details["current_sequence"] == 103
    assert details["rtp_timestamp_step"] == 160
    assert details["expected_rtp_timestamp_step"] == 160
    assert details["rtp_timestamp_continuous"] is True
    assert details["delta_ms"] == 146.0
    assert details["expected_ptime_ms"] == 20.0
    assert details["threshold_ms"] == 60.0
    assert details["catch_up"]["status"] == "FULL"
    assert details["catch_up"]["accelerated_interval_count"] > 0
    assert details["catch_up"]["profile_thresholds"]["max_following_packets"] == 8
    assert stream["lost_packets"] == 0
    assert stream["high_delta_without_sequence_loss_count"] == 1
    assert stream["high_delta_catch_up_count"] == 1


def test_high_delta_with_sequence_gap_is_not_semantically_mislabeled_as_no_loss():
    packets = [
        _packet(1, 0.000, 100, 0),
        _packet(2, 0.020, 101, 160),
        _packet(3, 0.040, 102, 320),
        _packet(4, 0.186, 104, 640),  # seq=103 absent
        _packet(5, 0.206, 105, 800),
        _packet(6, 0.226, 106, 960),
    ]
    stream = _stream(packets)
    high_delta = next(event for event in stream["events"] if event["type"] == "HIGH_DELTA")
    loss = next(event for event in stream["events"] if event["type"] == "PACKET_LOSS")

    details = high_delta["details"]
    assert details["classification"] == "INTERARRIVAL_STALL_WITH_SEQUENCE_GAP"
    assert details["loss_semantics"] == "SEQUENCE_GAP_PRESENT_AT_EVENT_BOUNDARY"
    assert details["sequence_step"] == 2
    assert details["sequence_continuous"] is False
    assert details["sequence_gap_packets"] == 1
    assert stream["lost_packets"] == 1
    assert loss["details"]["lost_packets"] == 1
    assert stream["high_delta_without_sequence_loss_count"] == 0


def test_high_delta_semantic_thresholds_are_versioned_in_analyzer_profile():
    profile = get_default_analyzer_profile()
    rtp = profile.section("rtp")

    assert profile.version == "1.3.0"
    assert int(rtp["high_delta_catch_up_max_packets"]) == 8
    assert float(rtp["high_delta_catch_up_accelerated_ratio"]) == 0.75
    assert float(rtp["high_delta_catch_up_full_recovery_ratio"]) == 0.80
    assert float(rtp["high_delta_catch_up_partial_recovery_ratio"]) == 0.10
    assert float(rtp["high_delta_timestamp_tolerance_ratio"]) == 0.05
