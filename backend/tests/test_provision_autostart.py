"""Provision -> auto Case + ReproductionSession + start (Feishu gap closed) tests.

Verifies that a successful device provision auto-creates a Case and a
ReproductionSession for the provisioned DUT and starts autonomous reproduction
(no more manual Web case creation).
"""
from __future__ import annotations

from pathlib import Path
import tempfile
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import Case, CaseDevice, DeviceCredential, ReproductionSession


def _engine():
    eng = create_engine('sqlite+pysqlite:///:memory:')
    Base.metadata.create_all(eng)
    return eng


def _seed_credential(db: Session, *, sn='SN-1', ip='10.44.77.254', port=2222):
    cred = DeviceCredential(sn=sn, ip=ip, ssh_port=port, username='root',
                            password='pw', source='poseidon')
    db.add(cred)
    db.commit()
    return cred


def test_autostart_creates_case_and_session_and_starts(monkeypatch):
    from app.workers.device_provision_task import _autostart_reproduction
    eng = _engine()
    with Session(eng) as db:
        _seed_credential(db)
        started = {}
        def fake_apply_async(args, queue=None):
            started['session_id'] = args[0]
        import app.db.session as dbs
        monkeypatch.setattr(dbs, 'SessionLocal', lambda: Session(eng))
        monkeypatch.setattr('app.workers.reproduction_tasks.start_reproduction.apply_async', fake_apply_async, raising=False)
        # Real SessionLocal inside _autostart_reproduction is patched; registry needs
        # the real profiles dir, so run with real orchestrator against the mem DB.
        result = _autostart_reproduction(sn='SN-1', product='APF1250')
        assert result['started'] is True
        assert result['case_id']
        assert result['session_id']
        case = db.get(Case, result['case_id'])
        assert case is not None
        dev = db.scalar(select(CaseDevice).where(CaseDevice.case_id == case.id, CaseDevice.sn == 'SN-1'))
        assert dev is not None and dev.ip == '10.44.77.254' and dev.ssh_port == 2222
        sess = db.get(ReproductionSession, result['session_id'])
        assert sess is not None
        assert started.get('session_id') == result['session_id']


def test_autostart_reuses_existing_case_for_sn(monkeypatch):
    from app.workers.device_provision_task import _autostart_reproduction
    eng = _engine()
    with Session(eng) as db:
        _seed_credential(db)
        # Pre-create a Case + device for the SN.
        case = Case(case_no='C-1', summary='existing', status='NEW')
        db.add(case); db.flush()
        db.add(CaseDevice(case_id=case.id, ip='10.44.77.254', ssh_port=2222, sn='SN-1', username='root'))
        db.commit()
        import app.db.session as dbs
        monkeypatch.setattr(dbs, 'SessionLocal', lambda: Session(eng))
        monkeypatch.setattr('app.workers.reproduction_tasks.start_reproduction.apply_async',
                            lambda args, queue=None: None, raising=False)
        result = _autostart_reproduction(sn='SN-1', product='APF1250')
        assert result['started'] is True
        assert result['case_id'] == case.id  # reused, not a new case


def test_autostart_missing_credential_returns_not_found(monkeypatch):
    from app.workers.device_provision_task import _autostart_reproduction
    eng = _engine()
    with Session(eng) as db:
        import app.db.session as dbs
        monkeypatch.setattr(dbs, 'SessionLocal', lambda: Session(eng))
        result = _autostart_reproduction(sn='NO-SUCH-SN', product=None)
        assert result['started'] is False
        assert result['reason'] == 'DEVICE_CREDENTIAL_NOT_FOUND'
