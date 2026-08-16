"""Feishu long-connection listener + shared event dispatch tests.

Covers the shared dispatch_event carrying the source chat_id into provision, the
SDK payload normalisation (P2ImMessageReceiveV1 -> dispatch_event shape), and
card-action toast responses. The official lark-oapi SDK owns the WebSocket frame
protocol (bootstrap / challenge / ping / protobuf), so the frame-level tests are
replaced by tests of our message payload adapter.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base


def _engine():
    eng = create_engine(
        'sqlite+pysqlite:///:memory:',
        poolclass=StaticPool,
        connect_args={'check_same_thread': False},
    )
    Base.metadata.create_all(eng)
    return eng


def test_dispatch_event_passes_chat_id_to_provision(monkeypatch):
    from app.integrations.feishu.events import dispatch_event
    eng = _engine()
    with Session(eng) as db:
        dispatched = {}
        def fake_apply_async(args, queue=None):
            dispatched['args'] = args
            dispatched['queue'] = queue
        monkeypatch.setattr('app.workers.device_provision_task.provision_from_feishu.apply_async',
                            fake_apply_async, raising=False)
        payload = {
            'header': {'event_type': 'im.message.receive_v1', 'event_id': 'evt-1'},
            'event': {
                'chat_id': 'oc_group_A',
                'chat_type': 'group',
                'message': {'message_id': 'msg-1', 'root_id': 'root-1',
                            'content': json.dumps({'text': '单通无声，请排查 sn=SN-1 web=https://x.noc.rj.link/'})},
            },
        }
        result = dispatch_event(db, payload=payload)
        assert result['handled'] == 'diagnosis_intake_dispatched'
        assert result['chat_id'] == 'oc_group_A'
        assert result['chat_type'] == 'group'
        # provision_from_feishu(text, chat_id, chat_type): chat_id is 2nd,
        # chat_type is 3rd positional arg.
        assert dispatched['args'][1] == 'oc_group_A'
        assert dispatched['args'][2] == 'group'
        assert dispatched['args'][3]['event_id'] == 'evt-1'
        assert dispatched['args'][3]['message_id'] == 'msg-1'
        assert dispatched['args'][3]['root_message_id'] == 'root-1'
        assert dispatched['args'][4] is True  # Evidence First, no Generic reproduction


def test_dispatch_event_is_idempotent_by_feishu_event_id(monkeypatch):
    from app.integrations.feishu.events import dispatch_event
    eng = _engine()
    with Session(eng) as db:
        dispatched = {'n': 0}
        monkeypatch.setattr(
            'app.workers.device_provision_task.provision_from_feishu.apply_async',
            lambda args, queue=None: dispatched.__setitem__('n', dispatched['n'] + 1),
            raising=False,
        )
        payload = {
            'header': {'event_type': 'im.message.receive_v1', 'event_id': 'evt-same'},
            'event': {'chat_id': 'oc_A', 'chat_type': 'group',
                      'message': {'message_id': 'msg-same',
                                  'content': json.dumps({'text': '单通无声，请排查 sn=SN-1 ip=10.0.0.1'})}},
        }
        first = dispatch_event(db, payload=payload)
        second = dispatch_event(db, payload=payload)
        assert first['handled'] == 'diagnosis_intake_dispatched'
        assert second['handled'] == 'diagnosis_intake_dispatched'
        assert second['duplicate'] is True
        assert dispatched['n'] == 1


def test_dispatch_event_no_text_does_not_provision(monkeypatch):
    from app.integrations.feishu.events import dispatch_event
    eng = _engine()
    with Session(eng) as db:
        called = {'n': 0}
        def fake_apply_async(args, queue=None):
            called['n'] += 1
        monkeypatch.setattr('app.workers.device_provision_task.provision_from_feishu.apply_async',
                            fake_apply_async, raising=False)
        payload = {
            'header': {'event_type': 'im.message.receive_v1'},
            'event': {'chat_id': 'oc_x', 'message': {'content': json.dumps({'text': '   '})}},
        }
        result = dispatch_event(db, payload=payload)
        assert result['handled'] == 'empty_text'
        assert called['n'] == 0


def _sdk_message(chat_id, text, sender_open_id='ou_1', chat_type='group'):
    """Build a fake P2ImMessageReceiveV1-shaped object (same accessor paths)."""
    return SimpleNamespace(
        header=SimpleNamespace(event_id='evt-sdk', tenant_key='tenant-sdk', create_time='123450'),
        event=SimpleNamespace(
            message=SimpleNamespace(
                chat_id=chat_id,
                chat_type=chat_type,
                content=json.dumps({'text': text}),
                message_type='text',
                message_id='msg-sdk',
                root_id='root-sdk',
                parent_id='parent-sdk',
                create_time='123456',
            ),
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id=sender_open_id)),
        )
    )


def test_message_payload_normalises_sdk_event():
    from app.integrations.feishu.long_connection import _message_payload
    data = _sdk_message('oc_group_A', 'OPEN_SSH sn=SN-1 web=https://x.noc.rj.link/')
    payload = _message_payload(data)
    assert payload['header']['event_type'] == 'im.message.receive_v1'
    assert payload['header']['event_id'] == 'evt-sdk'
    assert payload['header']['tenant_key'] == 'tenant-sdk'
    assert payload['header']['create_time'] == '123450'
    assert payload['event']['chat_id'] == 'oc_group_A'
    assert payload['event']['chat_type'] == 'group'
    assert payload['event']['message']['chat_id'] == 'oc_group_A'
    assert payload['event']['message']['chat_type'] == 'group'
    assert json.loads(payload['event']['message']['content'])['text'].startswith('OPEN_SSH')
    assert payload['event']['message']['message_id'] == 'msg-sdk'
    assert payload['event']['message']['root_id'] == 'root-sdk'
    assert payload['event']['message']['message_type'] == 'text'
    assert payload['operator']['open_id'] == 'ou_1'


def test_message_payload_p2p_carries_chat_type():
    from app.integrations.feishu.long_connection import _message_payload
    data = _sdk_message('oc_dm_1', 'OPEN_SSH sn=SN-1', sender_open_id='ou_1', chat_type='p2p')
    payload = _message_payload(data)
    assert payload['event']['chat_type'] == 'p2p'
    assert payload['event']['message']['chat_type'] == 'p2p'


def test_dispatch_event_p2p_passes_dm_chat_id_and_chat_type(monkeypatch):
    from app.integrations.feishu.events import dispatch_event
    eng = _engine()
    with Session(eng) as db:
        dispatched = {}
        def fake_apply_async(args, queue=None):
            dispatched['args'] = args
        monkeypatch.setattr('app.workers.device_provision_task.provision_from_feishu.apply_async',
                            fake_apply_async, raising=False)
        payload = {
            'header': {'event_type': 'im.message.receive_v1'},
            'event': {
                'chat_id': 'oc_dm_1',
                'chat_type': 'p2p',
                'message': {'content': json.dumps({'text': '杂音，请排查 sn=SN-1 ip=10.0.0.1'}), 'chat_id': 'oc_dm_1', 'chat_type': 'p2p'},
            },
        }
        result = dispatch_event(db, payload=payload)
        assert result['handled'] == 'diagnosis_intake_dispatched'
        assert result['chat_type'] == 'p2p'
        # p2p chat_id is the single-chat session id (oc_*), carried for
        # record-keeping; the binding still uses the chat_id receive type.
        assert dispatched['args'][1] == 'oc_dm_1'
        assert dispatched['args'][2] == 'p2p'


def test_dispatch_device_access_without_symptom_does_not_provision(monkeypatch):
    from app.integrations.feishu.events import dispatch_event
    eng = _engine()
    with Session(eng) as db:
        called = {'n': 0}
        monkeypatch.setattr('app.workers.device_provision_task.provision_from_feishu.apply_async',
                            lambda args, queue=None: called.__setitem__('n', called['n'] + 1), raising=False)
        payload = {
            'header': {'event_type': 'im.message.receive_v1', 'event_id': 'evt-no-symptom'},
            'event': {'chat_id': 'oc_A', 'chat_type': 'group',
                      'message': {'message_id': 'msg-no-symptom', 'message_type': 'text',
                                  'content': json.dumps({'text': '打开SSH sn=SN-1 ip=10.0.0.1'})}},
        }
        result = dispatch_event(db, payload=payload)
        assert result['handled'] == 'needs_clarification'
        assert 'symptom_description' in result['missing_user_inputs']
        assert called['n'] == 0


def test_dispatch_attachment_prioritizes_precheck_and_does_not_provision(monkeypatch):
    from app.integrations.feishu.events import dispatch_event
    eng = _engine()
    with Session(eng) as db:
        calls = {'attachment': 0, 'provision': 0}
        monkeypatch.setattr('app.workers.device_provision_task.ingest_feishu_attachments.apply_async',
                            lambda args, queue=None: calls.__setitem__('attachment', calls['attachment'] + 1), raising=False)
        monkeypatch.setattr('app.workers.device_provision_task.provision_from_feishu.apply_async',
                            lambda args, queue=None: calls.__setitem__('provision', calls['provision'] + 1), raising=False)
        payload = {
            'header': {'event_type': 'im.message.receive_v1', 'event_id': 'evt-file'},
            'event': {'chat_id': 'oc_A', 'chat_type': 'group',
                      'message': {'message_id': 'msg-file', 'message_type': 'file',
                                  'content': json.dumps({'file_key': 'fk-1', 'file_name': 'call.pcapng'})}},
        }
        result = dispatch_event(db, payload=payload)
        assert result['handled'] == 'attachment_precheck_dispatched'
        assert calls == {'attachment': 1, 'provision': 0}


def test_dispatch_p2p_attachment_keeps_private_chat_as_delivery_context(monkeypatch):
    from app.integrations.feishu.events import dispatch_event
    eng = _engine()
    with Session(eng) as db:
        dispatched = {}
        monkeypatch.setattr(
            'app.workers.device_provision_task.ingest_feishu_attachments.apply_async',
            lambda args, queue=None: dispatched.update(args=args, queue=queue), raising=False,
        )
        payload = {
            'header': {'event_type': 'im.message.receive_v1', 'event_id': 'evt-dm-file'},
            'event': {'chat_id': 'oc_dm_attachment', 'chat_type': 'p2p',
                      'message': {'message_id': 'msg-dm-file', 'message_type': 'file',
                                  'chat_id': 'oc_dm_attachment', 'chat_type': 'p2p',
                                  'content': json.dumps({'file_key': 'fk-dm', 'file_name': 'dm.pcap'})}},
        }
        result = dispatch_event(db, payload=payload)
        assert result['handled'] == 'attachment_precheck_dispatched'
        assert result['chat_type'] == 'p2p'
        assert dispatched['args'][1:3] == ['oc_dm_attachment', 'p2p']
        assert dispatched['args'][3]['chat_type'] == 'p2p'


def test_dispatch_p2p_status_query_resolves_case_from_private_thread():
    from app.integrations.feishu.events import dispatch_event
    from app.db.models import Case, FeishuCaseBinding
    eng = _engine()
    with Session(eng) as db:
        case = Case(case_no='VOIP-20260816-DM0001', summary='私聊单通', status='ANALYZING')
        db.add(case); db.flush()
        db.add(FeishuCaseBinding(
            case_id=case.id, receive_id='oc_dm_status', receive_id_type='chat_id',
            source_message_id='msg-root-dm', source_root_message_id='msg-root-dm',
            source_chat_type='p2p', status='ACTIVE', card_version=0,
        ))
        db.flush()
        payload = {
            'header': {'event_type': 'im.message.receive_v1', 'event_id': 'evt-dm-status'},
            'event': {'chat_id': 'oc_dm_status', 'chat_type': 'p2p',
                      'message': {'message_id': 'msg-status', 'root_id': 'msg-root-dm',
                                  'message_type': 'text',
                                  'content': json.dumps({'text': '现在诊断进度怎么样？'})}},
        }
        result = dispatch_event(db, payload=payload)
        assert result['handled'] == 'status_query'
        assert result['case_id'] == case.id
        assert result['case_status'] == 'ANALYZING'


def test_cross_thread_case_correlation_requires_device_and_specific_symptom(monkeypatch):
    from app.db.models import Case, CaseDevice, FeishuCaseBinding
    from app.integrations.feishu.events import dispatch_event

    eng = _engine()
    with Session(eng) as db:
        case = Case(case_no='VOIP-20260816-COR001', summary='偶发单通无声', status='ANALYZING')
        db.add(case); db.flush()
        db.add(CaseDevice(case_id=case.id, ip='10.0.0.8', ssh_port=22,
                          sn='SN-COR-1', username='root'))
        db.add(FeishuCaseBinding(case_id=case.id, receive_id='oc_cor',
                                 source_message_id='old-msg', source_root_message_id='old-root'))
        db.commit()
        dispatched = {}
        monkeypatch.setattr(
            'app.workers.device_provision_task.provision_from_feishu.apply_async',
            lambda args, queue=None: dispatched.update(args=args, queue=queue), raising=False,
        )
        payload = {
            'header': {'event_type': 'im.message.receive_v1', 'event_id': 'evt-cor-new'},
            'event': {'chat_id': 'oc_cor', 'chat_type': 'group',
                      'message': {'message_id': 'new-msg', 'root_id': 'new-root',
                                  'content': json.dumps({'text':
                                      '偶发单通无声，请继续排查 sn=SN-COR-1 ip=10.0.0.8'})}},
        }
        result = dispatch_event(db, payload=payload)
        assert result['case_id'] == case.id
        assert result['correlation_reason'] == 'DEVICE_SYMPTOM_TIME_WINDOW'
        assert dispatched['args'][3]['correlated_case_id'] == case.id


def test_cross_thread_ambiguous_candidates_are_not_guessed(monkeypatch):
    from app.db.models import Case, CaseDevice, FeishuCaseBinding
    from app.integrations.feishu.events import dispatch_event

    eng = _engine()
    with Session(eng) as db:
        for suffix in ('A', 'B'):
            case = Case(case_no=f'VOIP-20260816-COR00{suffix}', summary='单通无声', status='ANALYZING')
            db.add(case); db.flush()
            db.add(CaseDevice(case_id=case.id, ip='10.0.0.9', ssh_port=22,
                              sn='SN-SAME', username='root'))
            db.add(FeishuCaseBinding(case_id=case.id, receive_id='oc_amb',
                                     source_message_id=f'old-{suffix}'))
        db.commit()
        called = {'n': 0}
        monkeypatch.setattr(
            'app.workers.device_provision_task.provision_from_feishu.apply_async',
            lambda args, queue=None: called.__setitem__('n', called['n'] + 1), raising=False,
        )
        payload = {
            'header': {'event_type': 'im.message.receive_v1', 'event_id': 'evt-amb'},
            'event': {'chat_id': 'oc_amb', 'chat_type': 'group',
                      'message': {'message_id': 'new-amb',
                                  'content': json.dumps({'text':
                                      '单通无声，请排查 sn=SN-SAME ip=10.0.0.9'})}},
        }
        result = dispatch_event(db, payload=payload)
        assert result['handled'] == 'needs_case_disambiguation'
        assert len(result['candidate_case_nos']) == 2
        assert called['n'] == 0


def test_text_external_action_completion_advances_single_waiting_run(monkeypatch):
    from app.db.models import Case, DiagnosticExperiment, ExperimentRun, FeishuCaseBinding
    from app.integrations.feishu.events import dispatch_event

    eng = _engine()
    with Session(eng) as db:
        case = Case(case_no='VOIP-20260816-EXT001', summary='电源干扰', status='ROOT_CAUSE_CONFIRMED')
        db.add(case); db.flush()
        db.add(FeishuCaseBinding(case_id=case.id, receive_id='oc_ext',
                                 source_message_id='ext-root', source_root_message_id='ext-root'))
        experiment = DiagnosticExperiment(
            case_id=case.id, profile_key='P', profile_version='1', profile_checksum='x',
            effective_profile_snapshot={}, state='WAITING_EXTERNAL_ACTION',
            confirmation_policy='ABA', independent_variable='power',
            target_finding='NOISE', reproduction_profile_id='VOIP_GENERIC_FULL_CAPTURE',
        )
        db.add(experiment); db.flush()
        run = ExperimentRun(experiment_id=experiment.id, case_id=case.id, run_no=1,
                            variant='B', status='WAITING_EXTERNAL_ACTION',
                            external_action_required=True)
        db.add(run); db.commit()
        monkeypatch.setattr('app.integrations.feishu.events.enqueue_reply', lambda *args: True)
        monkeypatch.setattr('app.workers.device_provision_task.sync_case_card.apply_async',
                            lambda args, queue=None: None, raising=False)
        payload = {
            'header': {'event_type': 'im.message.receive_v1', 'event_id': 'evt-ext'},
            'event': {'chat_id': 'oc_ext', 'chat_type': 'group',
                      'message': {'message_id': 'ext-done', 'root_id': 'ext-root',
                                  'content': json.dumps({'text': '现场操作已完成'})}},
        }
        result = dispatch_event(db, payload=payload, actor='feishu:ou-test')
        assert result['handled'] == 'external_action_completed'
        assert run.status == 'READY'
        assert run.external_action_completed_at is not None


def test_text_fix_applied_records_fix_after_root_cause_confirmed(monkeypatch):
    from app.db.models import Case, FeishuCaseBinding, FixAction, Hypothesis
    from app.integrations.feishu.events import dispatch_event

    eng = _engine()
    with Session(eng) as db:
        case = Case(case_no='VOIP-20260816-FIX001', summary='配置异常',
                    status='ROOT_CAUSE_CONFIRMED')
        db.add(case); db.flush()
        db.add(FeishuCaseBinding(case_id=case.id, receive_id='oc_fix',
                                 source_message_id='fix-root', source_root_message_id='fix-root'))
        db.add(Hypothesis(case_id=case.id, code='CONFIG_ERROR', title='配置错误',
                          fault_domain='CONFIG', status='CONFIRMED', confidence=9000))
        db.commit()
        monkeypatch.setattr('app.integrations.feishu.events.enqueue_reply', lambda *args: True)
        monkeypatch.setattr('app.workers.device_provision_task.sync_case_card.apply_async',
                            lambda args, queue=None: None, raising=False)
        payload = {
            'header': {'event_type': 'im.message.receive_v1', 'event_id': 'evt-fix'},
            'event': {'chat_id': 'oc_fix', 'chat_type': 'group',
                      'message': {'message_id': 'fix-done', 'root_id': 'fix-root',
                                  'content': json.dumps({'text': '已修复，修改了语音配置参数'})}},
        }
        result = dispatch_event(db, payload=payload, actor='feishu:ou-test')
        assert result['handled'] == 'fix_applied'
        fix = db.get(FixAction, result['fix_action_id'])
        assert fix is not None and fix.action_type == 'CONFIG_CHANGE'
        assert case.status == 'RESOLVING'


def test_general_question_answers_only_from_verified_knowledge(monkeypatch):
    from app.db.models import KnowledgeItem
    from app.integrations.feishu.events import dispatch_event

    eng = _engine()
    with Session(eng) as db:
        db.add(KnowledgeItem(
            type='PROTOCOL_GUIDE', title='SIP 401 认证挑战',
            summary='401 后携带认证信息重试并最终成功，属于正常认证流程。',
            tags_json=['SIP', '401'], source_ref='reviewed:sip-401',
            verified=1, status='ACTIVE',
        ))
        db.commit()
        replies = []
        monkeypatch.setattr('app.integrations.feishu.events.enqueue_reply',
                            lambda message_id, text: replies.append(text) or True)
        payload = {
            'header': {'event_type': 'im.message.receive_v1', 'event_id': 'evt-qa'},
            'event': {'chat_id': 'oc_qa', 'chat_type': 'p2p',
                      'message': {'message_id': 'qa-1',
                                  'content': json.dumps({'text': 'SIP 401 是什么？'})}},
        }
        result = dispatch_event(db, payload=payload)
        assert result['handled'] == 'general_question'
        assert result['answered'] is True
        assert result['citations'][0]['source_ref'] == 'reviewed:sip-401'
        assert '已审核知识库' in replies[-1]


def test_general_question_without_verified_match_fails_closed(monkeypatch):
    from app.integrations.feishu.events import dispatch_event
    eng = _engine()
    with Session(eng) as db:
        replies = []
        monkeypatch.setattr('app.integrations.feishu.events.enqueue_reply',
                            lambda message_id, text: replies.append(text) or True)
        payload = {
            'header': {'event_type': 'im.message.receive_v1', 'event_id': 'evt-qa-none'},
            'event': {'chat_id': 'oc_qa', 'chat_type': 'p2p',
                      'message': {'message_id': 'qa-none',
                                  'content': json.dumps({'text': '量子传送语音是什么？'})}},
        }
        result = dispatch_event(db, payload=payload)
        assert result['handled'] == 'general_question'
        assert result['answered'] is False
        assert result['citations'] == []
        assert '没有找到' in replies[-1]


def test_message_payload_empty_sender_has_no_operator():
    from app.integrations.feishu.long_connection import _message_payload
    data = SimpleNamespace(event=SimpleNamespace(
        message=SimpleNamespace(chat_id='oc_x', content='{"text":"hi"}'),
        sender=None,
    ))
    payload = _message_payload(data)
    assert payload['event']['chat_id'] == 'oc_x'
    assert 'operator' not in payload


def test_message_payload_missing_event_is_empty():
    from app.integrations.feishu.long_connection import _message_payload
    payload = _message_payload(SimpleNamespace(event=None))
    assert payload['event']['chat_id'] == ''
    assert payload['event']['message']['content'] == ''


def test_run_long_connection_raises_when_live_disabled(monkeypatch):
    from app.integrations.feishu.long_connection import (
        FeishuLongConnectionError,
        run_long_connection,
    )
    from app.core.config import settings
    monkeypatch.setattr(settings, 'feishu_live_enabled', False)
    try:
        run_long_connection()
        assert False, 'expected FeishuLongConnectionError'
    except FeishuLongConnectionError as exc:
        assert str(exc) == 'FEISHU_LIVE_DISABLED'


def test_runner_skips_when_live_disabled(monkeypatch):
    from app.workers.feishu_long_connection_task import feishu_long_connection
    from app.core.config import settings
    monkeypatch.setattr(settings, 'feishu_live_enabled', False)
    result = feishu_long_connection.apply().get()
    assert result['status'] == 'SKIPPED'
    assert result['reason'] == 'FEISHU_LIVE_DISABLED'


# -- card.action.trigger (v2) / card.action.trigger_v1 (legacy) responses --------


def test_card_action_trigger_returns_toast_for_open_case(monkeypatch):
    # v2 card.action.trigger: user taps a card button -> callback answers with a
    # toast (immediate feedback) instead of a bare {"code":0,"msg":"ok"}.
    from app.integrations.feishu.events import dispatch_event
    eng = _engine()
    with Session(eng) as db:
        payload = {
            'header': {'event_type': 'card.action.trigger', 'event_id': 'e1'},
            'event': {'action': {'value': {'action': 'OPEN_CASE', 'case_id': 'c1'}},
                      'operator': {'open_id': 'ou_1'}},
        }
        result = dispatch_event(db, payload=payload)
        assert result['handled'] == 'open_case'
        assert result['toast']['type'] == 'info'
        assert '网页' in result['toast']['content']


def test_card_action_trigger_v1_returns_toast_for_stop(monkeypatch):
    # Legacy card.action.trigger_v1: action lives at top level (not under event).
    from app.integrations.feishu.events import dispatch_event
    from app.db.models import Case, CaseDevice, ReproductionSession
    eng = _engine()
    with Session(eng) as db:
        case = Case(case_no='CARD-V1', summary='v1', status='ANALYZING')
        db.add(case); db.flush()
        dev = CaseDevice(case_id=case.id, ip='192.0.2.1', ssh_port=22, sn='V1', username='root')
        db.add(dev); db.flush()
        sess = ReproductionSession(case_id=case.id, device_id=dev.id, profile_key='P',
                                   profile_version='1', profile_checksum='c', effective_profile_snapshot={},
                                   state='WATCHING')
        db.add(sess); db.commit()
        dispatched = {'n': 0}
        monkeypatch.setattr('app.workers.reproduction_tasks.cancel_reproduction.apply_async',
                            lambda args, queue=None: dispatched.__setitem__('n', dispatched['n'] + 1), raising=False)
        payload = {
            'type': 'card.action.trigger_v1',
            'action': {'value': {'action': 'STOP_REPRODUCTION', 'session_id': sess.id}},
        }
        result = dispatch_event(db, payload=payload)
        assert result['handled'] == 'stop_reproduction'
        assert result['toast']['type'] == 'info'
        assert '停止' in result['toast']['content']
        assert dispatched['n'] == 1


def test_card_action_trigger_error_returns_error_toast():
    from app.integrations.feishu.events import dispatch_event
    eng = _engine()
    with Session(eng) as db:
        payload = {
            'header': {'event_type': 'card.action.trigger'},
            'event': {'action': {'value': {'action': 'STOP_REPRODUCTION', 'session_id': 'missing'}}},
        }
        result = dispatch_event(db, payload=payload)
        assert result['handled'] == 'error'
        assert result['toast']['type'] == 'error'


def test_http_callback_returns_toast_for_card_action(monkeypatch):
    # The HTTP callback endpoint surfaces the toast for card action-trigger events.
    from app.integrations.feishu.events import dispatch_event
    eng = _engine()
    with Session(eng) as db:
        payload = {
            'header': {'event_type': 'card.action.trigger'},
            'event': {'action': {'value': {'action': 'OPEN_CASE'}}},
        }
        result = dispatch_event(db, payload=payload)
        # simulate the callback wrapper's card-action branch
        from app.integrations.feishu.events import CARD_ACTION_EVENT_TYPES
        event_type = payload['header']['event_type']
        if event_type in CARD_ACTION_EVENT_TYPES:
            response = {'code': 0, 'msg': 'ok', 'toast': result.get('toast') or {}}
        else:
            response = {'code': 0, 'msg': 'ok'}
        assert response['toast']['type'] == 'info'
