from app.reports.evidence_brief import build_report_payload
from app.reports.prd_spec_v1_alignment import build_evidence_completeness


def _packet(*, sip_messages: int = 7, rtp_streams: int = 3) -> dict:
    streams = [
        {
            "stream_id": f"rtp-{idx}",
            "src_ip": "192.168.3.200",
            "src_port": 13294 + idx,
            "dst_ip": "192.168.150.10",
            "dst_port": 10000,
            "packet_count": 10,
            "unique_packet_count": 10,
            "duplicate_packets": 0,
            "lost_packets": 0,
            "codec": "PCMU",
            "ptime_ms": 20,
        }
        for idx in range(rtp_streams)
    ]
    return {
        "summary": {
            "packet_count": 100,
            "sip_message_count": sip_messages,
            "call_count": 1 if sip_messages else 0,
            "rtp_stream_count": rtp_streams,
            "rtcp_report_count": 0,
        },
        "calls": [{"call_id": "call-1"}] if sip_messages else [],
        "rtp_streams": streams,
    }


def _pcm() -> dict:
    return {
        "summary": {"stream_count": 2},
        "streams": [
            {"tap": {"name": "pcm_rx"}, "packet_count": 10, "sessions": []},
            {"tap": {"name": "pcm_tx"}, "packet_count": 10, "sessions": []},
        ],
    }


def _payload(results: dict) -> dict:
    return build_report_payload(
        case={"case_no": "VOIP-GOLDEN", "summary": "single-way audio"},
        scope_type="CASE",
        scope_id="VOIP-GOLDEN",
        session=None,
        call=None,
        environment=None,
        evidences=[{"type": "PCAP"}],
        analyzer_states={"media_intelligence": {"status": "SUCCESS", "run_id": "media-1"}},
        results=results,
        report_version=1,
    )


def test_media_intelligence_embedded_packet_pcm_feed_report_completeness() -> None:
    payload = _payload({
        "media_intelligence": {
            "packet": _packet(),
            "pcm": _pcm(),
            "summary": {},
            "correlations": [],
        }
    })

    assert payload["packet_summary"]["available"] is True
    assert payload["packet_summary"]["sip_message_count"] == 7
    assert payload["packet_summary"]["rtp_stream_count"] == 3
    assert payload["pcm_summary"]["available"] is True
    assert {row["tap"]["name"] for row in payload["pcm_summary"]["streams"]} == {"pcm_rx", "pcm_tx"}

    completeness = build_evidence_completeness(payload)
    dimensions = completeness["dimensions"]
    assert dimensions["PCAP"]["available"] is True
    assert dimensions["SIP"]["available"] is True
    assert dimensions["RTP"]["available"] is True
    assert dimensions["PCM_RX"]["available"] is True
    assert dimensions["PCM_TX"]["available"] is True
    assert dimensions["CORRELATION"]["available"] is True
    assert dimensions["DEBUG"]["available"] is False
    assert completeness["missing_required"] == []


def test_independent_packet_pcm_results_keep_precedence_over_media_embedded_results() -> None:
    independent_packet = _packet(sip_messages=11, rtp_streams=1)
    independent_pcm = {
        "summary": {"stream_count": 1},
        "streams": [{"tap": {"name": "pcm_tx"}, "packet_count": 4, "sessions": []}],
    }
    payload = _payload({
        "packet_intelligence": independent_packet,
        "pcm_intelligence": independent_pcm,
        "media_intelligence": {
            "packet": _packet(sip_messages=7, rtp_streams=3),
            "pcm": _pcm(),
            "summary": {},
            "correlations": [],
        },
    })

    assert payload["packet_summary"]["sip_message_count"] == 11
    assert payload["packet_summary"]["rtp_stream_count"] == 1
    assert {row["tap"]["name"] for row in payload["pcm_summary"]["streams"]} == {"pcm_tx"}
