from app.contracts.evidence_report import AnalysisMode, CallOrigin, CallScope
from app.reports.evidence_brief import build_report_payload, render_report_html
from app.services.evidence_report_context import (
    CALL_BINDING_INCOMPLETE,
    FULLY_REVIEWABLE,
    NOT_FULLY_REVIEWABLE,
    REPORT_SEMANTIC_CONTRADICTION,
    resolve_report_analysis_context,
)


FIELD_CALL_ID = "00ad1c804c33b255@192.168.3.200"


def _packet_call(
    call_id: str,
    start: float,
    end: float,
    *,
    state: str = "TERMINATED",
    caller: str = "sip:8000@192.168.3.200",
    callee: str = "sip:601@192.168.3.200",
    bidirectional: bool = True,
) -> dict:
    return {
        "call_id": call_id,
        "caller": caller,
        "callee": callee,
        "state": state,
        "start_time": start,
        "end_time": end,
        "media_start_time": start + 1.0,
        "media_end_time": end - 1.0,
        "invite_final_status": 200,
        "rtp_stream_ids": [f"rtp-{call_id}-up", f"rtp-{call_id}-down"],
        "capture_completeness": {"is_partial": False},
        "media_direction_health": {"status": "BIDIRECTIONAL" if bidirectional else "UNKNOWN"},
    }


def _packet_result(calls: list[dict], *, rtp_streams: list[dict] | None = None) -> dict:
    streams = rtp_streams if rtp_streams is not None else [
        {"stream_id": "up", "src_ip": "192.168.150.4", "src_port": 10000, "dst_ip": "192.168.3.200", "dst_port": 11446},
        {"stream_id": "down", "src_ip": "192.168.3.200", "src_port": 11446, "dst_ip": "192.168.150.4", "dst_port": 10000},
    ]
    return {
        "summary": {
            "packet_count": 100,
            "sip_message_count": 8,
            "call_count": len(calls),
            "rtp_stream_count": len(streams),
            "rtcp_report_count": 0,
        },
        "calls": calls,
        "rtp_streams": streams,
        "anomalies": [],
    }


def _states_all_success() -> dict:
    return {
        "packet_intelligence": {"status": "SUCCESS", "run_id": "packet-run", "analyzer_version": "1", "config_version": "v"},
        "pcm_intelligence": {"status": "SUCCESS", "run_id": "pcm-run", "analyzer_version": "1", "config_version": "v"},
        "media_intelligence": {"status": "SUCCESS", "run_id": "media-run", "analyzer_version": "1", "config_version": "v"},
    }


def test_offline_pcap_reconstructs_call_and_dialed_number():
    packet = _packet_result([_packet_call(FIELD_CALL_ID, 100.0, 120.0)])
    results = {"packet_intelligence": packet, "pcm_intelligence": None, "media_intelligence": None}

    resolved = resolve_report_analysis_context(
        scope_type="CASE",
        session=None,
        runtime_call=None,
        evidences=[{"type": "PCAP", "source": "USER_UPLOAD"}],
        results=results,
    )
    context = resolved["analysis_context"]
    call = resolved["display_call"]

    assert context["analysis_mode"] == AnalysisMode.OFFLINE_IMPORTED.value
    assert context["capture_origin"] == "USER_UPLOAD"
    assert context["call_origin"] == CallOrigin.RECONSTRUCTED_FROM_PCAP.value
    assert context["call_scope"] == CallScope.BOUND.value
    assert context["reconstructed_call_count"] == 1
    assert context["semantic_issues"] == []
    assert context["reviewability"] == FULLY_REVIEWABLE
    assert call["id"] == "CALL-001"
    assert call["sip_call_id"] == FIELD_CALL_ID
    assert call["caller"] == "8000"
    assert call["dialed_number"] == "601"
    assert call["status"] == "TERMINATED"
    assert call["started_at"] == "1970-01-01T00:01:40+00:00"
    assert call["ended_at"] == "1970-01-01T00:02:00+00:00"
    assert call["origin"] == CallOrigin.RECONSTRUCTED_FROM_PCAP.value


def test_offline_context_selects_latest_reconstructed_call_deterministically():
    packet = _packet_result([
        _packet_call("older", 10.0, 20.0),
        _packet_call("latest", 30.0, 50.0),
        _packet_call("middle", 25.0, 40.0),
    ])
    results = {"packet_intelligence": packet, "pcm_intelligence": None, "media_intelligence": None}

    resolved = resolve_report_analysis_context(
        scope_type="CASE", session=None, runtime_call=None, evidences=[{"type": "PCAP"}], results=results
    )
    context = resolved["analysis_context"]
    call = resolved["display_call"]

    assert context["reconstructed_call_count"] == 3
    assert context["selected_sip_call_id"] == "latest"
    assert context["selection_rule"] == "LATEST_RECONSTRUCTED_CALL_BY_END_THEN_START_TIME"
    assert call["sip_call_id"] == "latest"
    assert call["id"] == "CALL-002"


def test_runtime_call_remains_runtime_context_and_authority():
    runtime_session = {"id": "session-1", "status": "COMPLETED"}
    runtime_call = {
        "id": "db-call-1",
        "call_no": 7,
        "external_call_ref": "field-call",
        "status": "ENDED",
        "started_at": "2026-08-20T01:00:00+00:00",
        "ended_at": "2026-08-20T01:01:00+00:00",
    }
    packet = _packet_result([_packet_call("packet-call", 100.0, 200.0)])
    results = {"packet_intelligence": packet, "pcm_intelligence": None, "media_intelligence": None}

    resolved = resolve_report_analysis_context(
        scope_type="CALL",
        session=runtime_session,
        runtime_call=runtime_call,
        evidences=[{"type": "PCAP", "source": "REPRODUCTION"}],
        results=results,
    )
    context = resolved["analysis_context"]
    call = resolved["display_call"]

    assert context["analysis_mode"] == AnalysisMode.REPRODUCTION.value
    assert context["call_origin"] == CallOrigin.REPRODUCTION_RUNTIME.value
    assert context["call_scope"] == CallScope.BOUND.value
    assert context["semantic_issues"] == []
    assert call["id"] == "db-call-1"
    assert call["origin"] == CallOrigin.REPRODUCTION_RUNTIME.value
    assert call["source"]["type"] == CallOrigin.REPRODUCTION_RUNTIME.value


def test_case_unbound_uploaded_pcap_does_not_inherit_historical_runtime_call():
    runtime_session = {"id": "historical-session", "status": "COMPLETED"}
    runtime_call = {
        "id": "historical-db-call",
        "call_no": 9,
        "external_call_ref": "historical-sip-call",
        "status": "ENDED",
        "started_at": "2026-08-19T01:00:00+00:00",
        "ended_at": "2026-08-19T01:01:00+00:00",
    }
    packet = _packet_result([_packet_call(FIELD_CALL_ID, 100.0, 120.0)])
    results = {"packet_intelligence": packet, "pcm_intelligence": None, "media_intelligence": None}

    resolved = resolve_report_analysis_context(
        scope_type="CASE",
        session=runtime_session,
        runtime_call=runtime_call,
        evidences=[{
            "type": "PCAP",
            "source": "USER_UPLOAD",
            "session_id": None,
            "call_id": None,
        }],
        results=results,
    )
    context = resolved["analysis_context"]
    call = resolved["display_call"]

    assert context["analysis_mode"] == AnalysisMode.OFFLINE_IMPORTED.value
    assert context["source_session_id"] is None
    assert context["historical_runtime_session_id"] == "historical-session"
    assert context["historical_runtime_call_id"] == "historical-db-call"
    assert context["runtime_context_suppressed_for_unbound_case_capture"] is True
    assert context["call_origin"] == CallOrigin.RECONSTRUCTED_FROM_PCAP.value
    assert call["sip_call_id"] == FIELD_CALL_ID
    assert call["id"] != "historical-db-call"
    assert context["semantic_issues"] == []


def test_offline_rtp_only_packet_is_unbound_and_does_not_fabricate_call():
    packet = _packet_result([], rtp_streams=[
        {"stream_id": "rtp-only", "src_ip": "10.0.0.1", "src_port": 10000, "dst_ip": "10.0.0.2", "dst_port": 20000}
    ])
    results = {"packet_intelligence": packet, "pcm_intelligence": None, "media_intelligence": None}

    resolved = resolve_report_analysis_context(
        scope_type="CASE", session=None, runtime_call=None, evidences=[{"type": "PCAP"}], results=results
    )
    context = resolved["analysis_context"]

    assert resolved["display_call"] is None
    assert context["analysis_mode"] == AnalysisMode.OFFLINE_IMPORTED.value
    assert context["call_origin"] == CallOrigin.MEDIA_SESSION_UNBOUND.value
    assert context["call_scope"] == CallScope.UNBOUND.value
    assert context["reconstructed_call_count"] == 0
    assert context["semantic_issues"] == []


def test_offline_context_can_fallback_to_media_embedded_packet_result_without_reanalysis():
    packet = _packet_result([_packet_call("media-fallback", 10.0, 30.0)])
    results = {
        "packet_intelligence": None,
        "pcm_intelligence": None,
        "media_intelligence": {"packet": packet},
    }
    resolved = resolve_report_analysis_context(
        scope_type="CASE", session=None, runtime_call=None, evidences=[{"type": "PCAP"}], results=results
    )

    assert resolved["display_call"]["sip_call_id"] == "media-fallback"
    assert resolved["analysis_context"]["packet_call_source"] == "media_intelligence.packet"


def test_report_semantic_gate_detects_call_count_without_bindable_call_and_downgrades_reviewability():
    broken_call = _packet_call("", 10.0, 30.0)
    packet = _packet_result([broken_call])
    packet["summary"]["call_count"] = 1
    results = {
        "packet_intelligence": packet,
        "pcm_intelligence": {"streams": [
            {"tap": {"name": "pcm_rx"}, "sessions": []},
            {"tap": {"name": "pcm_tx"}, "sessions": []},
        ]},
        "media_intelligence": {"summary": {}},
    }
    resolved = resolve_report_analysis_context(
        scope_type="CASE", session=None, runtime_call=None, evidences=[{"type": "PCAP"}], results=results
    )
    context = resolved["analysis_context"]

    assert resolved["display_call"] is None
    assert REPORT_SEMANTIC_CONTRADICTION in context["semantic_issues"]
    assert CALL_BINDING_INCOMPLETE in context["semantic_issues"]
    assert context["reviewability"] == NOT_FULLY_REVIEWABLE

    payload = build_report_payload(
        case={"id": "c", "case_no": "C-1", "summary": "offline pcap", "status": "OPEN"},
        scope_type="CASE",
        scope_id="c",
        session=None,
        call=None,
        analysis_context=context,
        display_call=None,
        environment={},
        evidences=[{"type": "PCAP"}, {"type": "PCM_RX"}, {"type": "PCM_TX"}],
        analyzer_states=_states_all_success(),
        results=results,
        report_version=1,
        generated_at="2026-08-20T00:00:00+00:00",
    )
    assert payload["completeness"]["state"] == "PARTIAL"
    assert payload["completeness"]["semantic_status"] == "INCOMPLETE"
    assert payload["completeness"]["reviewability"] == NOT_FULLY_REVIEWABLE


def test_offline_report_html_has_reconstructed_call_not_call_none_and_reproduction_is_not_applicable():
    packet = _packet_result([_packet_call(FIELD_CALL_ID, 100.0, 120.0)])
    results = {"packet_intelligence": packet, "pcm_intelligence": None, "media_intelligence": None}
    resolved = resolve_report_analysis_context(
        scope_type="CASE",
        session=None,
        runtime_call=None,
        evidences=[{"type": "PCAP", "source": "USER_UPLOAD"}],
        results=results,
    )
    payload = build_report_payload(
        case={"id": "c", "case_no": "C-1", "summary": "offline pcap", "status": "OPEN"},
        scope_type="CASE",
        scope_id="c",
        session=None,
        call=None,
        analysis_context=resolved["analysis_context"],
        display_call=resolved["display_call"],
        environment={},
        evidences=[{"type": "PCAP"}],
        analyzer_states={
            "packet_intelligence": {"status": "SUCCESS", "run_id": "packet-run", "analyzer_version": "1", "config_version": "v"},
            "pcm_intelligence": {"status": "UNAVAILABLE"},
            "media_intelligence": {"status": "UNAVAILABLE"},
        },
        results=results,
        report_version=1,
        generated_at="2026-08-20T00:00:00+00:00",
    )
    html = render_report_html(payload)

    assert payload["display_call"] is not None
    assert payload["call"] is not None
    assert "4. 当前离线 Call 重建结果" in html
    assert "分析方式：离线证据导入" in html
    assert "复现 Session：不适用" in html
    assert "Call：CALL-001" in html
    assert f"SIP Call-ID：{FIELD_CALL_ID}" in html
    assert "号码：8000 → 601" in html
    assert "状态：TERMINATED" in html
    assert "Call：None" not in html
    assert "系统不会为了填充报告而创建 ReproductionSession/ReproductionCall" in html
