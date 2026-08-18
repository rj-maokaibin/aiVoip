from app.contracts.evidence_report import REPORT_COMPOSER_VERSION, REPORT_SCHEMA_VERSION

def test_preliminary_evidence_report_schema_is_frozen_v1():
    assert REPORT_SCHEMA_VERSION=="preliminary-evidence-report-v1"
    assert REPORT_COMPOSER_VERSION=="evidence-brief-composer-v1"
