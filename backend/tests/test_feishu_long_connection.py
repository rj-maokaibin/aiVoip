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
            'header': {'event_type': 'im.message.receive_v1'},
            'event': {
                'chat_id': 'oc_group_A',
                'chat_type': 'group',
                'message': {'content': json.dumps({'text': 'OPEN_SSH sn=SN-1 web=https://x.noc.rj.link/'})},
            },
        }
        result = dispatch_event(db, payload=payload)
        assert result['handled'] == 'provision_dispatched'
        assert result['chat_id'] == 'oc_group_A'
        assert result['chat_type'] == 'group'
        # provision_from_feishu(text, chat_id, chat_type): chat_id is 2nd,
        # chat_type is 3rd positional arg.
        assert dispatched['args'][1] == 'oc_group_A'
        assert dispatched['args'][2] == 'group'


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
        event=SimpleNamespace(
            message=SimpleNamespace(
                chat_id=chat_id,
                chat_type=chat_type,
                content=json.dumps({'text': text}),
                message_type='text',
            ),
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id=sender_open_id)),
        )
    )


def test_message_payload_normalises_sdk_event():
    from app.integrations.feishu.long_connection import _message_payload
    data = _sdk_message('oc_group_A', 'OPEN_SSH sn=SN-1 web=https://x.noc.rj.link/')
    payload = _message_payload(data)
    assert payload['header']['event_type'] == 'im.message.receive_v1'
    assert payload['event']['chat_id'] == 'oc_group_A'
    assert payload['event']['chat_type'] == 'group'
    assert payload['event']['message']['chat_id'] == 'oc_group_A'
    assert payload['event']['message']['chat_type'] == 'group'
    assert json.loads(payload['event']['message']['content'])['text'].startswith('OPEN_SSH')
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
                'message': {'content': json.dumps({'text': 'OPEN_SSH sn=SN-1'}), 'chat_id': 'oc_dm_1', 'chat_type': 'p2p'},
            },
        }
        result = dispatch_event(db, payload=payload)
        assert result['handled'] == 'provision_dispatched'
        assert result['chat_type'] == 'p2p'
        # p2p chat_id is the single-chat session id (oc_*), carried for
        # record-keeping; the binding still uses the chat_id receive type.
        assert dispatched['args'][1] == 'oc_dm_1'
        assert dispatched['args'][2] == 'p2p'


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
