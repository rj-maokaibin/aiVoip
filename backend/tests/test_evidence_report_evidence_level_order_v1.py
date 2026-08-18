from app.contracts.evidence_report import EVIDENCE_LEVEL_ORDER

def test_evidence_level_order_prefers_current_direct_evidence():
    assert EVIDENCE_LEVEL_ORDER["L1"]>EVIDENCE_LEVEL_ORDER["L2"]>EVIDENCE_LEVEL_ORDER["L3"]>EVIDENCE_LEVEL_ORDER["L4"]>EVIDENCE_LEVEL_ORDER["L5"]
