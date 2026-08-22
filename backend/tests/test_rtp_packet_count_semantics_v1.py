from __future__ import annotations

from app.analyzers.packet.engine import PacketIntelligenceEngine
from app.analyzers.packet.rtp import RtpStreamAnalyzer
from app.analyzers.packet.types import NormalizedPacket, RtpData
from app.reports.evidence_brief import build_packet_summary


def _call() -> dict:
    return {
        "call_id": "dut-call",
        "state": "ESTABLISHED",
        "capture_completeness": {"is_partial": False},
        "rtp_stream_ids": ["up", "down"],
        "sdp": {
            "offer": {
                "media": [{
                    "media": "audio",
                    "connection_address": "192.168.150.4",
                    "port": 10000,
                    "direction": "sendrecv",
                }],
            },
            "answer": {
                "media": [{
                    "media": "audio",
                    "connection_address": "192.168.3.200",
                    "port": 11446,
                    "direction": "sendrecv",
                }],
            },
        },
    }


def _rtp_packet(frame: int, arrival: float, sequence: int, rtp_timestamp: int) -> NormalizedPacket:
    return NormalizedPacket(
        frame_number=frame,
        timestamp=arrival,
        src_ip="192.168.3.200",
        src_port=11446,
        dst_ip="192.168.150.4",
        dst_port=10000,
        transport="UDP",
        protocols=["rtp"],
        rtp=RtpData(
            ssrc=602295830,
            sequence=sequence,
            timestamp=rtp_timestamp,
            payload_type=0,
        ),
    )


def test_rtp_analyzer_retains_frame_level_duplicate_evidence_without_loss():
    stream = RtpStreamAnalyzer().analyze([
        _rtp_packet(10, 1.000000, 100, 16000),
        _rtp_packet(11, 1.020000, 101, 16160),
        _rtp_packet(12, 1.020150, 101, 16160),
    ])[0]

    assert stream["packet_count"] == 3
    assert stream["unique_packet_count"] == 2
    assert stream["expected_packets"] == 2
    assert stream["duplicate_packets"] == 1
    assert stream["lost_packets"] == 0
    assert stream["duplicate_events"] == [{
        "sequence": 101,
        "sequence_ext": 101,
        "first_frame_number": 11,
        "duplicate_frame_number": 12,
        "first_timestamp": 1.02,
        "duplicate_timestamp": 1.02015,
        "arrival_delta_ms": 0.15,
        "rtp_timestamp": 16160,
        "first_rtp_timestamp": 16160,
        "payload_type": 0,
        "first_payload_type": 0,
    }]


def test_media_direction_health_uses_unique_packets_not_duplicate_datagrams():
    call = _call()
    streams = [
        {
            "stream_id": "up",
            "src_ip": "192.168.150.4",
            "src_port": 10000,
            "dst_ip": "192.168.3.200",
            "dst_port": 11446,
            "packet_count": 20,
            "unique_packet_count": 20,
            "duplicate_packets": 0,
        },
        {
            "stream_id": "down",
            "src_ip": "192.168.3.200",
            "src_port": 11446,
            "dst_ip": "192.168.150.4",
            "dst_port": 10000,
            # Many duplicate datagrams must not manufacture reverse media.
            "packet_count": 50,
            "unique_packet_count": 0,
            "duplicate_packets": 50,
        },
    ]

    PacketIntelligenceEngine()._attach_media_direction_health([call], streams)
    health = call["media_direction_health"]

    assert health["packet_count_semantics"] == "UNIQUE_EFFECTIVE_RTP_PACKETS"
    assert health["a_to_b_packets"] == 20
    assert health["a_to_b_observed_packets"] == 20
    assert health["a_to_b_duplicate_packets"] == 0
    assert health["b_to_a_packets"] == 0
    assert health["b_to_a_observed_packets"] == 50
    assert health["b_to_a_duplicate_packets"] == 50
    assert health["status"] == "ONE_WAY_A_TO_B"


def test_report_packet_summary_separates_effective_observed_and_duplicate_counts():
    packet = {
        "summary": {
            "packet_count": 20419,
            "sip_message_count": 30,
            "call_count": 2,
            "rtp_stream_count": 4,
            "rtcp_report_count": 0,
        },
        "calls": [],
        "rtp_streams": [{
            "stream_id": "down",
            "src_ip": "192.168.3.200",
            "src_port": 11446,
            "dst_ip": "192.168.150.4",
            "dst_port": 10000,
            "ssrc": 602295830,
            "packet_count": 2427,
            "unique_packet_count": 2425,
            "duplicate_packets": 2,
            "expected_packets": 2425,
            "lost_packets": 0,
            "loss_rate": 0.0,
            "codec": "PCMU",
            "ptime_ms": 20.0,
        }],
    }

    row = build_packet_summary(packet)["streams"][0]
    assert row["packet_count_semantics"] == "UNIQUE_EFFECTIVE_RTP_PACKETS"
    assert row["packet_count"] == 2425
    assert row["unique_packet_count"] == 2425
    assert row["observed_packet_count"] == 2427
    assert row["duplicate_packets"] == 2
    assert row["lost_packets"] == 0
