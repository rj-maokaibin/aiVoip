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


def test_autostart_does_not_reuse_existing_case_by_sn_alone(monkeypatch):
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
        assert result['case_id'] != case.id


def test_autostart_reuses_case_only_for_same_feishu_thread(monkeypatch):
    from app.workers.device_provision_task import _autostart_reproduction
    eng = _engine()
    with Session(eng) as db:
        _seed_credential(db)
        import app.db.session as dbs
        monkeypatch.setattr(dbs, 'SessionLocal', lambda: Session(eng))
        monkeypatch.setattr('app.workers.reproduction_tasks.start_reproduction.apply_async',
                            lambda args, queue=None: None, raising=False)
        first = _autostart_reproduction(
            sn='SN-1', product='APF1250', chat_id='oc_group_A', chat_type='group',
            source_context={'event_id': 'evt-1', 'message_id': 'msg-1',
                            'root_message_id': 'root-1', 'sender_open_id': 'ou-1'},
        )
        second = _autostart_reproduction(
            sn='SN-1', product='APF1250', chat_id='oc_group_A', chat_type='group',
            source_context={'event_id': 'evt-2', 'message_id': 'msg-2',
                            'root_message_id': 'root-1', 'sender_open_id': 'ou-1'},
        )
        assert second['case_id'] == first['case_id']


def test_autostart_missing_credential_returns_not_found(monkeypatch):
    from app.workers.device_provision_task import _autostart_reproduction
    eng = _engine()
    with Session(eng) as db:
        import app.db.session as dbs
        monkeypatch.setattr(dbs, 'SessionLocal', lambda: Session(eng))
        result = _autostart_reproduction(sn='NO-SUCH-SN', product=None)
        assert result['started'] is False
        assert result['reason'] == 'DEVICE_CREDENTIAL_NOT_FOUND'


def test_autostart_binds_case_to_source_chat(monkeypatch):
    # A provisioned Case created from a Feishu message must be bound to the source
    # chat_id so the conclusion card returns to the SAME group (multi-group support).
    from app.workers.device_provision_task import _autostart_reproduction
    eng = _engine()
    with Session(eng) as db:
        _seed_credential(db)
        import app.db.session as dbs
        monkeypatch.setattr(dbs, 'SessionLocal', lambda: Session(eng))
        monkeypatch.setattr('app.workers.reproduction_tasks.start_reproduction.apply_async',
                            lambda args, queue=None: None, raising=False)
        result = _autostart_reproduction(
            sn='SN-1', product='APF1250', chat_id='oc_group_A',
            source_context={'event_id': 'evt-A', 'message_id': 'msg-A',
                            'root_message_id': 'root-A', 'sender_open_id': 'ou-A',
                            'tenant_key': 'tenant-A', 'create_time': '123456',
                            'normalized_text': '单通无声',
                            'attachments': [{'file_key': 'fk-A'}]},
        )
        assert result['started'] is True
        from app.db.models import FeishuCaseBinding
        binding = db.scalar(select(FeishuCaseBinding).where(FeishuCaseBinding.case_id == result['case_id']))
        assert binding is not None
        assert binding.receive_id == 'oc_group_A'
        assert binding.receive_id_type == 'chat_id'
        assert binding.message_id is None  # backfilled on first sync_case_card
        assert binding.source_event_id == 'evt-A'
        assert binding.source_message_id == 'msg-A'
        assert binding.source_root_message_id == 'root-A'
        assert binding.source_sender_open_id == 'ou-A'
        assert binding.source_tenant_key == 'tenant-A'
        assert binding.source_message_timestamp == '123456'
        assert binding.source_normalized_text == '单通无声'
        assert binding.source_attachment_refs == [{'file_key': 'fk-A'}]


def test_autostart_binds_case_to_source_dm_with_chat_id_type(monkeypatch):
    # A p2p (DM) message's chat_id is the single-chat session id (oc_*), not the
    # user's open_id, so the Case stays bound with receive_id_type='chat_id' -
    # that is what lets the conclusion card be pushed back to the DM.
    from app.workers.device_provision_task import _autostart_reproduction
    eng = _engine()
    with Session(eng) as db:
        _seed_credential(db)
        import app.db.session as dbs
        monkeypatch.setattr(dbs, 'SessionLocal', lambda: Session(eng))
        monkeypatch.setattr('app.workers.reproduction_tasks.start_reproduction.apply_async',
                            lambda args, queue=None: None, raising=False)
        result = _autostart_reproduction(sn='SN-1', product='APF1250', chat_id='oc_dm_1', chat_type='p2p')
        assert result['started'] is True
        from app.db.models import FeishuCaseBinding
        binding = db.scalar(select(FeishuCaseBinding).where(FeishuCaseBinding.case_id == result['case_id']))
        assert binding is not None
        assert binding.receive_id == 'oc_dm_1'
        assert binding.receive_id_type == 'chat_id'
        assert binding.message_id is None


def test_autostart_without_chat_has_no_binding(monkeypatch):
    # No chat_id supplied (e.g. API-created) -> no FeishuCaseBinding row.
    from app.workers.device_provision_task import _autostart_reproduction
    eng = _engine()
    with Session(eng) as db:
        _seed_credential(db)
        import app.db.session as dbs
        monkeypatch.setattr(dbs, 'SessionLocal', lambda: Session(eng))
        monkeypatch.setattr('app.workers.reproduction_tasks.start_reproduction.apply_async',
                            lambda args, queue=None: None, raising=False)
        result = _autostart_reproduction(sn='SN-1', product='APF1250', chat_id=None)
        assert result['started'] is True
        from app.db.models import FeishuCaseBinding
        binding = db.scalar(select(FeishuCaseBinding).where(FeishuCaseBinding.case_id == result['case_id']))
        assert binding is None


def test_evidence_first_creates_diagnosis_but_no_reproduction_session(monkeypatch):
    from app.workers.device_provision_task import _autostart_reproduction
    eng = _engine()
    with Session(eng) as db:
        _seed_credential(db)
        import app.db.session as dbs
        monkeypatch.setattr(dbs, 'SessionLocal', lambda: Session(eng))
        dispatched = {'diagnosis': 0, 'reproduction': 0}
        monkeypatch.setattr('app.workers.diagnosis_tasks.run_diagnosis.apply_async',
                            lambda args, queue=None: dispatched.__setitem__('diagnosis', dispatched['diagnosis'] + 1),
                            raising=False)
        monkeypatch.setattr('app.workers.reproduction_tasks.start_reproduction.apply_async',
                            lambda args, queue=None: dispatched.__setitem__('reproduction', dispatched['reproduction'] + 1),
                            raising=False)
        result = _autostart_reproduction(
            sn='SN-1', product='APF1250', chat_id='oc_A',
            source_context={'message_id': 'msg-A'},
            start_reproduction_session=False,
            case_summary='单通无声，请排查',
        )
        assert result['workflow'] == 'EVIDENCE_FIRST'
        assert result['session_id'] is None
        assert db.scalar(select(ReproductionSession).where(
            ReproductionSession.case_id == result['case_id'])) is None
        assert dispatched == {'diagnosis': 1, 'reproduction': 0}
