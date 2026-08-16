from app.integrations.feishu.feedback import (
    accepted_text, build_single_user_question, human_case_status,
)


def test_human_case_status_never_returns_raw_known_enum():
    assert human_case_status('ANALYZING') == '正在分析已有证据'
    assert human_case_status('WAITING_USER') == '等待您补充信息或完成现场操作'


def test_accepted_feedback_explains_evidence_first_order():
    text = accepted_text()
    assert '已受理' in text
    assert '先检查已有证据' in text


def test_single_question_maps_timestamp_without_internal_protocol_terms():
    decision = {'plan': [{
        'action_type': 'REQUEST_USER_EVIDENCE',
        'params': {'need': ['anomaly_timestamp_or_field_recording']},
    }]}
    question = build_single_user_question(decision=decision)
    assert '大致时间' in question and '不知道' in question
    assert all(word not in question.lower() for word in ('pcm', 'slic', 'aimd', 'sip', 'rtp'))


def test_single_question_fallback_has_three_explicit_answer_paths():
    question = build_single_user_question()
    assert question.count('？') == 1
    assert '可以 / 暂时不能 / 不确定' in question


def test_case_milestone_feedback_is_idempotent(monkeypatch):
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from app.core.config import settings
    from app.db.base import Base
    from app.db.models import Case, FeishuCaseBinding, IdempotencyRecord
    from app.integrations.feishu.feedback import notify_case_once

    engine = create_engine('sqlite+pysqlite:///:memory:')
    Base.metadata.create_all(engine)
    sent = []
    monkeypatch.setattr(settings, 'feishu_live_enabled', True)
    monkeypatch.setattr(
        'app.integrations.feishu.feedback.enqueue_reply',
        lambda message_id, text: sent.append((message_id, text)) or True,
    )
    with Session(engine) as db:
        case = Case(case_no='C-FEEDBACK-1', summary='单通无声')
        db.add(case)
        db.flush()
        db.add(FeishuCaseBinding(
            case_id=case.id, receive_id='oc_dm_1', source_message_id='om_source_1'
        ))
        db.commit()

        first = notify_case_once(
            db, case_id=case.id, feedback_type='COMPLETED', token='run-1', text='完成'
        )
        second = notify_case_once(
            db, case_id=case.id, feedback_type='COMPLETED', token='run-1', text='完成'
        )

        assert first['status'] == 'QUEUED'
        assert second['duplicate'] is True
        assert sent == [('om_source_1', '完成')]
        assert len(db.scalars(select(IdempotencyRecord).where(
            IdempotencyRecord.scope == 'FEISHU_CASE_FEEDBACK'
        )).all()) == 1


def test_follow_up_becomes_evidence_and_resumes_waiting_case(monkeypatch):
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import Session

    from app.db.base import Base
    from app.db.models import Case, Evidence
    from app.workers.device_provision_task import ingest_feishu_follow_up

    engine = create_engine('sqlite+pysqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        case = Case(case_no='C-FOLLOW-UP-1', summary='偶发单通无声', status='WAITING_USER')
        db.add(case)
        db.commit()
        case_id = case.id

    monkeypatch.setattr('app.db.session.SessionLocal', lambda: Session(engine))
    stored = {}
    monkeypatch.setattr(
        'app.integrations.storage.ObjectStorage.put_bytes',
        lambda _self, key, data, content_type: stored.update(
            key=key, data=data, content_type=content_type
        ),
    )
    resumed = []
    monkeypatch.setattr(
        'app.workers.diagnosis_tasks.notify_case_changed',
        lambda target: resumed.append(target),
    )
    context = {'message_id': 'om-follow-up-1', 'root_message_id': 'om-root-1',
               'sender_open_id': 'ou-user-1'}
    first = ingest_feishu_follow_up.run(case_id, '现在可以复现', context)
    second = ingest_feishu_follow_up.run(case_id, '现在可以复现', context)

    assert first['status'] == 'OK'
    assert second['duplicate'] is True
    assert stored['data'] == '现在可以复现'.encode('utf-8')
    assert resumed == [case_id]
    with Session(engine) as db:
        case = db.get(Case, case_id)
        assert '[用户补充] 现在可以复现' in case.summary
        assert db.scalar(select(func.count(Evidence.id)).where(
            Evidence.case_id == case_id, Evidence.type == 'USER_RESPONSE'
        )) == 1
