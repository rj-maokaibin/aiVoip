from app.reports.evidence_brief import build_report_payload


def test_preliminary_report_never_claims_root_cause_authority():
    results={"packet_intelligence":None,"pcm_intelligence":None,"media_intelligence":None}
    states={name:{"status":"UNAVAILABLE"} for name in results}
    payload=build_report_payload(case={"id":"c","case_no":"C-1","summary":"x","status":"OPEN"},scope_type="CASE",scope_id="c",
        session=None,call=None,environment={},evidences=[],analyzer_states=states,results=results,report_version=1,generated_at="2026-08-18T00:00:00+00:00")
    assert payload["evidence_boundary"]["root_cause_authority"]=="PRELIMINARY_EVIDENCE_ONLY"
    assert "不确认最终 Root Cause" in payload["evidence_boundary"]["statement"]
    assert "历史 Case" in payload["evidence_boundary"]["historical_case_authority"]
