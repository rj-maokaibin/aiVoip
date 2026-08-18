from app.contracts.evidence_report import FINDING_SIGNATURE_VERSION

def test_finding_signature_contract_is_versioned():
    assert FINDING_SIGNATURE_VERSION=="sig-v1"
