from app.db.evidence_report_models import EvidenceFinding, EvidenceReportArtifactLink, FeishuEvidenceDocumentBinding, PreliminaryEvidenceReport
from app.db.base import Base


def test_evidence_report_tables_are_registered():
    expected={"preliminary_evidence_reports","evidence_findings","evidence_report_artifact_links","feishu_evidence_document_bindings"}
    assert expected.issubset(set(Base.metadata.tables))
    assert PreliminaryEvidenceReport.__tablename__=="preliminary_evidence_reports"
    assert EvidenceFinding.__tablename__=="evidence_findings"
    assert EvidenceReportArtifactLink.__tablename__=="evidence_report_artifact_links"
    assert FeishuEvidenceDocumentBinding.__tablename__=="feishu_evidence_document_bindings"
