from app.contracts.evidence_report import EvidenceReportScope

def test_scope_values_are_unique():
    values=[x.value for x in EvidenceReportScope]
    assert len(values)==len(set(values))
