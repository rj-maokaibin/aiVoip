from app.integrations.feishu.evidence_document import FeishuEvidenceDocumentService

def test_d112_edit_interval_respects_feishu_document_edit_rate():
    assert FeishuEvidenceDocumentService.DOC_EDIT_INTERVAL_SECONDS >= 0.34
