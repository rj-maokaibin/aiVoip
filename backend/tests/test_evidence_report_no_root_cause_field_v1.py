from app.contracts.evidence_report import REPORT_SCHEMA_VERSION

def test_preliminary_schema_is_evidence_report_not_diagnosis_report():
    assert "evidence-report" in REPORT_SCHEMA_VERSION
    assert "diagnosis" not in REPORT_SCHEMA_VERSION
