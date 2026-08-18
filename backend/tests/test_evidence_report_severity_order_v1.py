from app.contracts.evidence_report import SEVERITY_ORDER

def test_severity_order_is_monotonic():
    assert SEVERITY_ORDER["INFO"]<SEVERITY_ORDER["MEDIUM"]<SEVERITY_ORDER["HIGH"]<SEVERITY_ORDER["CRITICAL"]
