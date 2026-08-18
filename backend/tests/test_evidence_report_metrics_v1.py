from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.evidence_report_models import FeishuEvidenceDocumentBinding, PreliminaryEvidenceReport
from app.db.models import AnalyzerRun, Case
from app.services.evidence_report_metrics import evidence_report_pipeline_metrics


def _engine():
    eng = create_engine('sqlite+pysqlite:///:memory:', poolclass=StaticPool, connect_args={'check_same_thread': False})
    Base.metadata.create_all(eng)
    return eng


def test_pipeline_metrics_report_latency_status_analyzers_and_feishu():
    eng = _engine()
    now = datetime.now(timezone.utc)
    with Session(eng) as db:
        case = Case(case_no='MET-1', summary='metrics', status='ANALYZING')
        db.add(case); db.flush()
        report = PreliminaryEvidenceReport(
            case_id=case.id, scope_type='CASE', scope_id=case.id, version=1, status='COMPLETE',
            schema_version='preliminary-evidence-report-v1', composer_version='test', input_snapshot_hash='a'*64,
            idempotency_key='b'*64, analyzer_versions_json={}, created_at=now-timedelta(seconds=2), completed_at=now,
        )
        db.add(report)
        db.add(AnalyzerRun(case_id=case.id, analyzer_name='packet_intelligence', analyzer_version='v1', status='SUCCESS', input_evidence_ids=[]))
        db.add(FeishuEvidenceDocumentBinding(case_id=case.id, status='SYNCED', projection_version=1, last_synced_at=now, created_at=now-timedelta(days=1)))
        db.flush()
        out = evidence_report_pipeline_metrics(db, window_days=30)
        assert out['schema_version'] == 'evidence-report-pipeline-metrics-v1'
        assert out['reports']['total'] == 1
        assert out['reports']['status']['COMPLETE'] == 1
        assert out['reports']['latency_seconds']['p50'] == 2.0
        assert out['analyzers']['status']['SUCCESS'] == 1
        assert out['feishu_projection']['status']['SYNCED'] == 1
        assert out['sla']['sample_count'] == 1
