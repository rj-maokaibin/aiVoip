from __future__ import annotations

from app.analyzers.packet.engine import PacketIntelligenceEngine
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
