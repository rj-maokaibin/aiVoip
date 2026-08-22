from __future__ import annotations

from types import SimpleNamespace

from app.reports.prd_spec_v1_alignment import build_call_formation_quality, finalize_report_contract


def _report():
    return SimpleNamespace(
        id="r-session", case_id="case-1", session_id="session-1", call_id=None,
        scope_type="SESSION", scope_id="session-1", version=1, status="COMPOSING",
    )


def _payload(call_count: int = 0, diagnostic_call_count: int = 0, display_call=None) -> dict:
    return {
        "schema_version": "preliminary-evidence-report-v1",
        "composer_version": "test",
        "report_version": 1,
        "scope": {"type": "SESSION", "id": "session-1"},
        "session": {"id": "session-1", "state": "COMPLETED"},
        "call": display_call,
        "display_call": display_call,
        "headline": "当前已完成的 Analyzer 未发现明显异常；该结论仅覆盖已采集且可分析的证据范围。",
        "completeness": {
            "state": "PARTIAL",
            "capture": {"pcap": True, "pcm_rx": False, "pcm_tx": False, "debug": True},
            "analyzers": {"media": {"available": False}},
        },
        "packet_summary": {
            "available": True,
            "sip_message_count": 0,
            "rtp_stream_count": 0,
            "calls": [],
            "streams": [],
        },
        "pcm_summary": {"available": False, "streams": []},
        "findings": [],
        "artifacts": [],
        "analysis_context": {
            "reviewability": "FULLY_REVIEWABLE",
            "semantic_issues": [],
            "diagnostic_call_count": diagnostic_call_count,
        },
        "multi_call_summary": {"call_count": call_count, "finding_groups": []},
        "evidence_boundary": {"statement": "本报告仅描述当前 Evidence。"},
        "normal_and_exclusion_evidence": [],
        "preliminary_assessment": {
            "summary": "当前已完成的 Analyzer 未发现明显异常。",
            "evidence_boundary": "本报告仅描述当前 Evidence。",
            "recommended_next_action": "继续复核。",
        },
    }


def test_fr028_session_without_valid_call_has_explicit_canonical_boundary():
    report = _report()
    payload = _payload()

    quality = build_call_formation_quality(report, payload)
    assert quality["status"] == "NO_VALID_CALL"
    assert quality["no_valid_call"] is True
    assert quality["preserved_pre_call_evidence"] == ["PCAP_PREROLL", "DEBUG"]

    finalize_report_contract(report, payload)
    assert payload["call_formation_quality"]["status"] == "NO_VALID_CALL"
    assert payload["analysis_context"]["session_call_status"] == "NO_VALID_CALL"
    assert "未形成有效 Call" in payload["headline"]
    assert "未形成有效 Call" in payload["preliminary_assessment"]["summary"]
    assert "不得解释为正常通话" in payload["evidence_boundary"]["statement"]
    assert "未发现明显异常" not in payload["headline"]
    assert payload["traceability"]["call_formation_boundary"] == "NO_VALID_CALL"


def test_fr028_does_not_trigger_when_session_has_call_report():
    report = _report()
    payload = _payload(call_count=1)
    finalize_report_contract(report, payload)
    assert payload["call_formation_quality"]["no_valid_call"] is False
    assert payload["analysis_context"].get("session_call_status") is None


def test_fr028_does_not_trigger_when_packet_reconstructs_diagnostic_call():
    report = _report()
    payload = _payload(diagnostic_call_count=1, display_call={"id": "CALL-001", "status": "ENDED"})
    finalize_report_contract(report, payload)
    assert payload["call_formation_quality"]["status"] == "VALID_CALL_OR_NOT_APPLICABLE"
