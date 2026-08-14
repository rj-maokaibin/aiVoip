"""Diagnosis DIAGNOSED -> auto generate_report + sync_case_card tests.

Closes the gap where a DIAGNOSED diagnosis run stopped at the DB state; the
diagnosis report was never auto-generated and the Feishu case card was never
pushed back with the fresh conclusion (both required a manual API call).
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import Case, CaseDevice, DiagnosisRun


def _engine():
    eng = create_engine(
        'sqlite+pysqlite:///:memory:',
        poolclass=StaticPool,
        connect_args={'check_same_thread': False},
    )
    Base.metadata.create_all(eng)
    return eng


def _seed(db: Session):
    case = Case(case_no='AD-1', summary='auto publish', status='ANALYZING')
    db.add(case); db.flush()
    dev = CaseDevice(case_id=case.id, ip='192.0.2.10', ssh_port=22, sn='SN-AD', username='root')
    db.add(dev); db.flush()
    run = DiagnosisRun(case_id=case.id, status='DIAGNOSED', cycle=1,
                       reasoner_name='deterministic', reasoner_version='0.1.0',
                       workflow_version='m4-v1')
    db.add(run); db.commit(); db.refresh(run)
    return case, run


def test_publish_generates_report_and_syncs_card(monkeypatch):
    from app.workers.diagnosis_tasks import _publish_diagnosed_artifacts
    eng = _engine()
    with Session(eng) as db:
        case, run = _seed(db)

        # Fake report generation -> returns a row-like object + payload.
        class _FakeRow:
            id = 'rep-1'
        calls = {'report': 0, 'sync': 0}
        def fake_generate_report(db, case_id, actor=None):
            calls['report'] += 1
            return _FakeRow(), {'headline': 'x'}
        monkeypatch.setattr('app.reports.diagnosis_report.generate_report', fake_generate_report)

        class _FakeBinding:
            message_id = 'msg-1'
        async def fake_sync(self, db, *, case_id, receive_id=None, receive_id_type=None):
            calls['sync'] += 1
            return _FakeBinding()
        monkeypatch.setattr('app.integrations.feishu.service.FeishuCaseCardService.sync_case_card', fake_sync)
        # Enable live Feishu so the sync path is exercised.
        monkeypatch.setattr('app.core.config.settings.feishu_live_enabled', True)

        out = _publish_diagnosed_artifacts(db, case_id=case.id, run_id=run.id)
        assert calls['report'] == 1
        assert calls['sync'] == 1
        assert out['report'].startswith('GENERATED:rep-1')
        assert out['feishu'] == 'SYNCED:msg-1'


def test_publish_skips_feishu_when_live_disabled(monkeypatch):
    from app.workers.diagnosis_tasks import _publish_diagnosed_artifacts
    eng = _engine()
    with Session(eng) as db:
        case, run = _seed(db)
        monkeypatch.setattr('app.core.config.settings.feishu_live_enabled', False)
        sync_called = {'n': 0}
        async def fake_sync(self, db, *, case_id, receive_id=None, receive_id_type=None):
            sync_called['n'] += 1
            raise AssertionError('should not call sync when disabled')
        monkeypatch.setattr('app.integrations.feishu.service.FeishuCaseCardService.sync_case_card', fake_sync)
        out = _publish_diagnosed_artifacts(db, case_id=case.id, run_id=run.id)
        assert out['feishu'] == 'SKIPPED:FEISHU_LIVE_DISABLED'
        assert sync_called['n'] == 0


def test_publish_never_raises_when_report_fails(monkeypatch):
    from app.workers.diagnosis_tasks import _publish_diagnosed_artifacts
    eng = _engine()
    with Session(eng) as db:
        case, run = _seed(db)
        def boom(db, case_id, actor=None):
            raise RuntimeError('STORAGE_DOWN')
        monkeypatch.setattr('app.reports.diagnosis_report.generate_report', boom)
        monkeypatch.setattr('app.core.config.settings.feishu_live_enabled', False)
        # Must not raise; reports failure but the run itself is already DIAGNOSED.
        out = _publish_diagnosed_artifacts(db, case_id=case.id, run_id=run.id)
        assert out['report'].startswith('FAILED:')
        assert out['feishu'] == 'SKIPPED:FEISHU_LIVE_DISABLED'


def test_publish_never_raises_when_feishu_sync_fails(monkeypatch):
    from app.workers.diagnosis_tasks import _publish_diagnosed_artifacts
    eng = _engine()
    with Session(eng) as db:
        case, run = _seed(db)
        def fake_generate_report(db, case_id, actor=None):
            class _R:
                id = 'rep-2'
            return _R(), {}
        monkeypatch.setattr('app.reports.diagnosis_report.generate_report', fake_generate_report)
        async def boom(self, db, *, case_id, receive_id=None, receive_id_type=None):
            raise RuntimeError('FEISHU_API_DOWN')
        monkeypatch.setattr('app.integrations.feishu.service.FeishuCaseCardService.sync_case_card', boom)
        monkeypatch.setattr('app.core.config.settings.feishu_live_enabled', True)
        out = _publish_diagnosed_artifacts(db, case_id=case.id, run_id=run.id)
        assert out['report'].startswith('GENERATED:rep-2')
        assert out['feishu'].startswith('FAILED:')
