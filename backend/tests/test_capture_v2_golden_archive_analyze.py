from pathlib import Path

from app.capture_v2.gate import golden_archive_analyze as gaa


def test_parse_tcpdump_stats_reads_standard_summary():
    text = """
19547 packets captured
19547 packets received by filter
0 packets dropped by kernel
"""
    assert gaa.parse_tcpdump_stats(text) == {
        "packets_captured": 19547,
        "packets_received_by_filter": 19547,
        "packets_dropped_by_kernel": 0,
    }


def test_parse_tcpdump_stats_is_explicit_when_summary_missing():
    assert gaa.parse_tcpdump_stats("tcpdump: listening on br-voice") == {
        "packets_captured": None,
        "packets_received_by_filter": None,
        "packets_dropped_by_kernel": None,
    }


def test_rtp_continuity_accepts_wrap_and_counts_cross_segment_gap():
    rows = [
        (1.00, 65534, "a.pcap"),
        (1.02, 65535, "a.pcap"),
        (1.04, 0, "b.pcap"),
        (1.06, 3, "b.pcap"),
    ]
    result = gaa._rtp_continuity(rows)
    assert result["packet_count"] == 4
    assert result["estimated_missing_packets"] == 2
    assert result["cross_segment_transitions"] == 1
    assert result["cross_segment_missing_packets"] == 0
    assert result["backward_or_reordered_events"] == 0


def test_rtp_continuity_counts_gap_at_rotation_boundary():
    result = gaa._rtp_continuity([
        (1.00, 100, "a.pcap"),
        (1.02, 101, "a.pcap"),
        (1.04, 104, "b.pcap"),
    ])
    assert result["estimated_missing_packets"] == 2
    assert result["cross_segment_missing_packets"] == 2


def test_rtp_continuity_does_not_turn_backward_jump_into_huge_loss():
    result = gaa._rtp_continuity([
        (1.00, 1000, "a.pcap"),
        (1.02, 999, "a.pcap"),
    ])
    assert result["estimated_missing_packets"] == 0
    assert result["backward_or_reordered_events"] == 1


def test_safe_member_rejects_absolute_and_parent_traversal():
    assert gaa._safe_member_name("v21_golden/a.pcap") is True
    assert gaa._safe_member_name("/tmp/a.pcap") is False
    assert gaa._safe_member_name("v21_golden/../escape.pcap") is False


def test_analyze_requires_previously_recovered_fixed_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(gaa, "RECOVERY_ROOT", Path(tmp_path))
    result = gaa.analyze_archive(
        device_id="dev-1",
        model="APF1250",
        archive_date="20260820",
    )
    assert result["verdict"] == "INCONCLUSIVE"
    assert result["reason"] == "GOLDEN_ARCHIVE_NOT_RECOVERED"
    assert result["expected_local_path"].endswith(
        "dev-1/20260820/v21_golden_APF1250_20260820.tar.gz"
    )
