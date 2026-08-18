from app.contracts.evidence_report import DEFAULT_ROOT_CAUSE_BOUNDARY

def test_report_boundary_requires_current_case_confirmation_path():
    assert "当前 Case" in DEFAULT_ROOT_CAUSE_BOUNDARY
    assert "人工/修复验证" in DEFAULT_ROOT_CAUSE_BOUNDARY
