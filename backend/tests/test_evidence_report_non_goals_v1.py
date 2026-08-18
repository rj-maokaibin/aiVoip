from app.contracts.evidence_report import DEFAULT_ROOT_CAUSE_BOUNDARY

def test_preliminary_report_boundary_rejects_physical_root_cause_confirmation():
    assert "最终根因" in DEFAULT_ROOT_CAUSE_BOUNDARY
    assert "L1/L2" in DEFAULT_ROOT_CAUSE_BOUNDARY
