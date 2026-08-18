from app.contracts.evidence_report import REPORT_SCHEMA_VERSION

def test_report_baseline_remains_v1():
    assert REPORT_SCHEMA_VERSION.endswith("-v1")
