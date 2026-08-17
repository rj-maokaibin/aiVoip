from app.contracts.evidence_report import EvidenceReportScope

def test_evidence_report_scopes_are_call_session_case():
    assert {x.value for x in EvidenceReportScope}=={"CALL","SESSION","CASE"}
