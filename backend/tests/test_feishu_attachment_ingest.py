from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import DiagnosisRun, Evidence, ReproductionSession


def _engine():
    return create_engine(
        'sqlite+pysqlite:///:memory:', poolclass=StaticPool,
        connect_args={'check_same_thread': False},
    )


def test_attachment_ingest_creates_evidence_and_diagnosis_without_reproduction(monkeypatch):
    from app.workers.device_provision_task import ingest_feishu_attachments
    eng = _engine()
    Base.metadata.create_all(eng)

    class FakeTransport:
        async def download_message_resource(self, **kwargs):
            assert kwargs['message_id'] == 'msg-file'
            return SimpleNamespace(data=b'pcap-bytes', content_type='application/vnd.tcpdump.pcap')

    stored = {}
    class FakeStorage:
        def put_bytes(self, key, data, content_type):
            stored[key] = (data, content_type)

    import app.db.session as dbs
    monkeypatch.setattr(dbs, 'SessionLocal', lambda: Session(eng))
    monkeypatch.setattr('app.integrations.feishu.transport.FeishuLiveTransport', FakeTransport)
    monkeypatch.setattr('app.integrations.storage.ObjectStorage', FakeStorage)
    dispatched = {'n': 0}
    monkeypatch.setattr('app.workers.diagnosis_tasks.run_diagnosis.apply_async',
                        lambda args, queue=None: dispatched.__setitem__('n', dispatched['n'] + 1),
                        raising=False)
    replies = []
    monkeypatch.setattr('app.integrations.feishu.feedback.enqueue_reply',
                        lambda message_id, text: replies.append((message_id, text)) or True)

    result = ingest_feishu_attachments.run(
        '单通无声', 'oc_A', 'group',
        {'message_id': 'msg-file', 'root_message_id': 'root-file'},
        [{'file_key': 'fk-1', 'filename': 'call.pcap', 'message_type': 'file',
          'resource_type': 'file'}],
    )

    assert result['status'] == 'OK'
    assert result['reproduction_started'] is False
    assert dispatched['n'] == 1
    assert replies and replies[-1][0] == 'msg-file'
    with Session(eng) as db:
        evidence = db.scalar(select(Evidence))
        assert evidence is not None and evidence.type == 'PCAP'
        assert evidence.source == 'FEISHU_ATTACHMENT'
        assert db.scalar(select(DiagnosisRun)) is not None
        assert db.scalar(select(ReproductionSession)) is None
    assert next(iter(stored.values()))[0] == b'pcap-bytes'


def test_attachment_ingest_keeps_successes_and_reports_failed_files(monkeypatch):
    from app.workers.device_provision_task import ingest_feishu_attachments
    eng = _engine()
    Base.metadata.create_all(eng)

    class FakeTransport:
        async def download_message_resource(self, **kwargs):
            if kwargs['file_key'] == 'fk-bad':
                raise RuntimeError('expired resource')
            return SimpleNamespace(data=b'good-pcap', content_type='application/vnd.tcpdump.pcap')

    class FakeStorage:
        def put_bytes(self, key, data, content_type):
            return None

    import app.db.session as dbs
    monkeypatch.setattr(dbs, 'SessionLocal', lambda: Session(eng))
    monkeypatch.setattr('app.integrations.feishu.transport.FeishuLiveTransport', FakeTransport)
    monkeypatch.setattr('app.integrations.storage.ObjectStorage', FakeStorage)
    monkeypatch.setattr('app.workers.diagnosis_tasks.run_diagnosis.apply_async',
                        lambda args, queue=None: None, raising=False)
    replies = []
    monkeypatch.setattr('app.integrations.feishu.feedback.enqueue_reply',
                        lambda message_id, text: replies.append(text) or True)

    result = ingest_feishu_attachments.run(
        '单通无声', 'oc_A', 'group',
        {'message_id': 'msg-partial', 'root_message_id': 'root-partial'},
        [
            {'file_key': 'fk-good', 'filename': 'good.pcap', 'message_type': 'file',
             'resource_type': 'file'},
            {'file_key': 'fk-bad', 'filename': 'bad.pcap', 'message_type': 'file',
             'resource_type': 'file'},
        ],
    )

    assert result['status'] == 'PARTIAL_SUCCESS'
    assert len(result['evidence_ids']) == 1
    assert result['failed_attachments'][0]['filename'] == 'bad.pcap'
    assert '重新发送' in replies[-1] and 'bad.pcap' in replies[-1]
    with Session(eng) as db:
        assert len(list(db.scalars(select(Evidence)))) == 1


def test_attachment_ingest_all_failed_does_not_create_case(monkeypatch):
    from app.workers.device_provision_task import ingest_feishu_attachments
    eng = _engine()
    Base.metadata.create_all(eng)

    class FakeTransport:
        async def download_message_resource(self, **kwargs):
            raise RuntimeError('resource unavailable')

    import app.db.session as dbs
    monkeypatch.setattr(dbs, 'SessionLocal', lambda: Session(eng))
    monkeypatch.setattr('app.integrations.feishu.transport.FeishuLiveTransport', FakeTransport)
    replies = []
    monkeypatch.setattr('app.integrations.feishu.feedback.enqueue_reply',
                        lambda message_id, text: replies.append(text) or True)
    result = ingest_feishu_attachments.run(
        '单通无声', 'oc_A', 'group', {'message_id': 'msg-failed'},
        [{'file_key': 'fk-bad', 'filename': 'bad.pcap', 'message_type': 'file',
          'resource_type': 'file'}],
    )
    assert result['status'] == 'FAILED'
    assert result['reason'] == 'ALL_ATTACHMENTS_FAILED'
    assert '重新发送' in replies[-1]
    from app.db.models import Case
    with Session(eng) as db:
        assert db.scalar(select(Case)) is None
