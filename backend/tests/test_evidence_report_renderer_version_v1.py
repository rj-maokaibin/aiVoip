from app.contracts.evidence_report import RENDERER_VERSION


def test_evidence_renderer_has_versioned_contract():
    assert RENDERER_VERSION == "evidence-renderer-v2"
