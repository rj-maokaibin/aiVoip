from __future__ import annotations

from app.analyzers.pcm.engine import PcmIntelligenceEngine
from app.analyzers.pcm.pcap_udp import UdpDatagram


def _packet(frame: int, ts: float, src_ip: str, src_port: int) -> UdpDatagram:
    return UdpDatagram(
        frame_number=frame,
        timestamp=ts,
        src_ip=src_ip,
        dst_ip="192.168.3.200",
        src_port=src_port,
        dst_port=40000,
        payload=b"\x00" * 160,
    )


def test_pcm_source_endpoint_summary_is_deterministic_and_counted():
    packets = [
        _packet(1, 1.00, "192.168.150.4", 48741),
        _packet(2, 1.01, "192.168.150.4", 48741),
        _packet(3, 1.02, "192.168.150.4", 48741),
        _packet(4, 1.03, "192.168.150.9", 49999),
    ]
    endpoints = PcmIntelligenceEngine._source_endpoints(packets)
    assert endpoints[0] == {
        "ip": "192.168.150.4",
        "port": 48741,
        "packet_count": 3,
        "first_timestamp": 1.0,
        "last_timestamp": 1.02,
    }
    assert endpoints[1]["ip"] == "192.168.150.9"
    assert endpoints[1]["packet_count"] == 1


def test_pcm_provenance_contract_version_is_bumped():
    assert PcmIntelligenceEngine.analyzer_version == "0.6.0"
