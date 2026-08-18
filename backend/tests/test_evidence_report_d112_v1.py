from app.integrations.feishu.evidence_document import FeishuEvidenceDocumentService

def test_d112_edit_rate_is_below_three_document_edits_per_second():
    assert FeishuEvidenceDocumentService.DOC_EDIT_INTERVAL_SECONDS >= 1/3
