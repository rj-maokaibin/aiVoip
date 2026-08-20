from app.reports.evidence_brief import build_report_payload, render_report_html
from app.services.evidence_report_context import (
    ANALYSIS_MODE_OFFLINE_EVIDENCE,
    ANALYSIS_MODE_REPRODUCTION,
    CALL_SOURCE_PACKET_RECONSTRUCTION,
    CALL_SOURCE_REPRODUCTION,
    build_analysis_context,
)


def _packet_call(call_id: str, start: float, end: float, *, state: str = "TERMINATED") -> dict:
    return {
        "call_id": call_id,
        "state": state,
        "start_time": start,
        "end_time": end,
        "media_start_time": start + 1.0,
        "media_end_time": end - 1.0,
        "invite_final_status": 200,
        "rtp_stream_ids": [f"rtp-{call_id}-up", f"rtp-{call_id}-down"],
        "capture_completeness": {"is_partial": False},
        "media_direction_health": {"status": "BIDIRECTIONAL"},
    }


def test_offline_case_projects_packet_reconstructed_call_into_report_context():
    results = {
        "packet_intelligence": {"calls": [_packet_call("sip-call-1", 100.0, 120.0)]},
        "pcm_intelligence": None,
        "media_intelligence": None,
    }
    context = build_analysis_context(
        scope_type="CASE",
        session=None,
        reproduction_call=None,
        results=results,
    )

    assert context["mode"] == ANALYSIS_MODE_OFFLINE_EVIDENCE
    assert context["offline"] is True
    assert context["call_source"] == CALL_SOURCE_PACKET_RECONSTRUCTION
    assert context["reconstructed_call_count"] == 1
    assert context["call"]["external_call_ref"] == "sip-call-1"
    assert context["call"]["status"] == "TERMINATED"
    assert context["call"]["call_no"] == "PCAP-1"
    assert context["call"]["rtp_stream_ids"] == ["rtp-sip-call-1-up", "rtp-sip-call-1-down"]
    assert context["call"]["started_at"] == "1970-01-01T00:01:40+00:00"
    assert context["call"]["ended_at"] == "1970-01-01T00:02:00+00:00"


def test_offline_context_selects_latest_reconstructed_call_deterministically():
    results = {
        "packet_intelligence": {
            "calls": [
                _packet_call("older", 10.0, 20.0),
                _packet_call("latest", 30.0, 50.0),
                _packet_call("middle", 25.0, 40.0),
            ]
        },
        "pcm_intelligence": None,
        "media_intelligence": None,
    }
    context = build_analysis_context(scope_type="CASE", session=None, reproduction_call=None, results=results)

    assert context["reconstructed_call_count"] == 3
    assert context["selected_sip_call_id"] == "latest"
    assert context["call"]["external_call_ref"] == "latest"
    assert context["call"]["call_no"] == "PCAP-2"
    assert context["selection_rule"] == "LATEST_RECONSTRUCTED_CALL_BY_END_THEN_START_TIME"


def test_reproduction_call_remains_authoritative_over_packet_reconstruction():
    reproduction_call = {
        "id": "db-call-1",
        "call_no": 7,
        "external_call_ref": "field-call",
        "status": "ENDED",
        "started_at": "2026-08-20T01:00:00+00:00",
        "ended_at": "2026-08-20T01:01:00+00:00",
    }
    results = {
        "packet_intelligence": {"calls": [_packet_call("packet-call", 100.0, 200.0)]},
        "pcm_intelligence": None,
        "media_intelligence": None,
    }
    context = build_analysis_context(
        scope_type="CALL",
        session={"id": "session-1"},
        reproduction_call=reproduction_call,
        results=results,
    )

    assert context["mode"] == ANALYSIS_MODE_REPRODUCTION
    assert context["offline"] is False
    assert context["call_source"] == CALL_SOURCE_REPRODUCTION
    assert context["call"]["id"] == "db-call-1"
    assert context["call"]["source"]["type"] == CALL_SOURCE_REPRODUCTION
    assert context["reconstructed_call_count"] == 0


def test_offline_context_can_fallback_to_media_embedded_packet_result():
    results = {
        "packet_intelligence": None,
        "pcm_intelligence": None,
        "media_intelligence": {"packet": {"calls": [_packet_call("media-fallback", 10.0, 30.0)]}},
    }
    context = build_analysis_context(scope_type="CASE", session=None, reproduction_call=None, results=results)

    assert context["call"]["external_call_ref"] == "media-fallback"
    assert context["packet_call_source"] == "media_intelligence.packet"


def test_offline_context_does_not_fabricate_call_when_packet_has_no_call_id():
    results = {
        "packet_intelligence": {"calls": [{"state": "TERMINATED", "start_time": 1.0, "end_time": 2.0}]},
        "pcm_intelligence": None,
        "media_intelligence": None,
    }
    context = build_analysis_context(scope_type="CASE", session=None, reproduction_call=None, results=results)

    assert context["mode"] == ANALYSIS_MODE_OFFLINE_EVIDENCE
    assert context["call"] is None
    assert context["call_source"] is None
    assert context["selection_rule"] == "NO_RECONSTRUCTABLE_PACKET_CALL"


def test_reconstructed_call_removes_call_none_contradiction_from_html():
    packet = {
        "summary": {
            "packet_count": 100,
            "sip_message_count": 8,
            "call_count": 1,
            "rtp_stream_count": 2,
            "rtcp_report_count": 0,
        },
        "calls": [_packet_call("html-call", 100.0, 120.0)],
        "rtp_streams": [],
        "anomalies": [],
    }
    results = {
        "packet_intelligence": packet,
        "pcm_intelligence": None,
        "media_intelligence": None,
    }
    context = build_analysis_context(scope_type="CASE", session=None, reproduction_call=None, results=results)
    states = {
        "packet_intelligence": {"status": "SUCCESS", "run_id": "packet-run", "analyzer_version": "1", "config_version": "v"},
        "pcm_intelligence": {"status": "UNAVAILABLE"},
        "media_intelligence": {"status": "UNAVAILABLE"},
    }
    payload = build_report_payload(
        case={"id": "c", "case_no": "C-1", "summary": "offline pcap", "status": "OPEN"},
        scope_type="CASE",
        scope_id="c",
        session=context["session"],
        call=context["call"],
        environment={},
        evidences=[{"type": "PCAP"}],
        analyzer_states=states,
        results=results,
        report_version=1,
        generated_at="2026-08-20T00:00:00+00:00",
    )
    payload["analysis_context"] = context
    html = render_report_html(payload)

    assert payload["call"] is not None
    assert "Call：PCAP-1" in html
    assert "状态：TERMINATED" in html
    assert "Call：None" not in html
