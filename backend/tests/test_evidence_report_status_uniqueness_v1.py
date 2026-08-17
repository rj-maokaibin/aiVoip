from app.contracts.evidence_report import EvidenceReportStatus

def test_report_status_values_are_unique():
    values=[x.value for x in EvidenceReportStatus]
    assert len(values)==len(set(values))
