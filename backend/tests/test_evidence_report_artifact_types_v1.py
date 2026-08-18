from app.contracts.evidence_report import EvidenceReportArtifactType

def test_evidence_report_artifact_type_values_are_unique():
    values=[x.value for x in EvidenceReportArtifactType]
    assert len(values)==len(set(values))
