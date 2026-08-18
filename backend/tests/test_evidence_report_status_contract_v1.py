from app.contracts.evidence_report import EvidenceFindingSeverity, EvidenceFindingStatus, EvidenceReportStatus

def test_evidence_report_and_finding_status_contracts_are_complete():
    assert {x.value for x in EvidenceReportStatus}=={"PENDING","ANALYZING","COMPOSING","COMPLETE","PARTIAL_COMPLETE","FAILED","SUPERSEDED"}
    assert {x.value for x in EvidenceFindingStatus}=={"PROPOSED","OBSERVED","PERSISTING","RESOLVED","REVISED","INVALIDATED"}
    assert {x.value for x in EvidenceFindingSeverity}=={"INFO","MEDIUM","HIGH","CRITICAL"}
