from __future__ import annotations

from types import SimpleNamespace

from app.reports.prd_spec_v1_alignment import build_call_completion_quality, finalize_report_contract


def _report():
    return SimpleNamespace(
        id="r1", case_id="c1", session_id="s1", call_id="call1",
        scope_type="CALL", scope_id="call1", version=1, status="COMPOSING",
    )


def _complete_payload(call: dict) -> dict:
    return {
        "schema_version": "preliminary-evidence-report-v1",
        "composer_version": "test",
        "report_version": 1,
        "scope": {"type": "CALL", "id": "call1"},
        "call": call,
        "display_call": call,
        "completeness": {
            "state": "COMPLETE",
            "capture": {"pcap": True, "pcm_rx": True, "pcm_tx": True, "debug": True},
            "analyzers": {"media": {"available": True}},
        },
        "packet_summary": {
            "available": True,
            "sip_message_count": 6,
            "rtp_stream_count": 2,
            "calls": [{"call_id": "sip-call"}],
            "streams": [{"stream_id": "rtp-1"}, {"stream_id": "rtp-2"}],
        },
        "pcm_summary": {
            "available": True,
            "streams": [
                {"tap": {"name": "pcm_rx"}, "sessions": []},
                {"tap": {"name": "pcm_tx"}, "sessions": []},
            ],
        },
        "findings": [],
        "artifacts": [],
        "analysis_context": {"reviewability": "FULLY_REVIEWABLE", "semantic_issues": []},
        "evidence_boundary": {"statement": "原始边界。"},
        "normal_and_exclusion_evidence": [],
    }


def test_fr029_aborted_call_is_partial_and_never_claims_whole_call():
    report = _report()
    payload = _complete_payload({"status": "ABORTED", "ended_at": "2026-08-22T00:00:00Z", "incomplete": False})
    quality = build_call_completion_quality(report, payload)
    assert quality["boundary_downgraded"] is True

    finalize_report_contract(report, payload)
    assert report.status == "PARTIAL_COMPLETE"
    assert payload["status"] == "PARTIAL_COMPLETE"
    assert payload["call_completion_quality"]["status"] == "INCOMPLETE"
    assert "CALL_LIFECYCLE_INCOMPLETE" in payload["analysis_context"]["semantic_issues"]
    assert payload["analysis_context"]["reviewability"] == "NOT_FULLY_REVIEWABLE"
    assert "仅对已观测时间段" in payload["evidence_boundary"]["statement"]


def test_fr029_missing_end_is_partial_even_when_status_not_explicitly_aborted():
    report = _report()
    payload = _complete_payload({"status": "ACTIVE", "ended_at": None, "incomplete": True})
    finalize_report_contract(report, payload)
    assert payload["call_completion_quality"]["boundary_downgraded"] is True
    assert payload["status"] == "PARTIAL_COMPLETE"


def test_complete_call_does_not_trigger_fr029_boundary():
    report = _report()
    payload = _complete_payload({"status": "ANALYZED", "ended_at": "2026-08-22T00:00:00Z", "incomplete": False})
    finalize_report_contract(report, payload)
    assert payload["call_completion_quality"]["boundary_downgraded"] is False
    assert payload["status"] == "COMPLETE"
