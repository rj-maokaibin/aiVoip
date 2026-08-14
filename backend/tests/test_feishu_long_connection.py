"""Feishu long-connection listener + shared event dispatch tests.

Covers the shared dispatch_event carrying the source chat_id into provision, and
the WebSocket frame handler replying to challenge/ping and dispatching event
frames, so an intranet deployment can receive Feishu events without a public
callback URL.
"""
from __future__ import annotations

import json

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


class _FakeWS:
    """Records sent frames; lets the test push received frames."""

    def __init__(self):
        self.sent = []

    async def send(self, data: str):
        self.sent.append(json.loads(data))

    async def recv(self):
        return ''


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
                'message': {'content': json.dumps({'text': 'OPEN_SSH sn=SN-1 web=https://x.noc.rj.link/'})},
            },
        }
        result = dispatch_event(db, payload=payload)
        assert result['handled'] == 'provision_dispatched'
        assert result['chat_id'] == 'oc_group_A'
        # provision_from_feishu(text, chat_id) -> chat_id is 2nd positional arg
        assert dispatched['args'][1] == 'oc_group_A'


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


def test_handle_frame_challenge_and_ping():
    from app.integrations.feishu.long_connection import _handle_frame
    ws = _FakeWS()
    import asyncio
    async def noop():
        return None
    tag = asyncio.run(_handle_frame(ws, {'type': 'challenge', 'data': {'challenge': 'abc'}},
                                    on_event=lambda d: noop()))
    assert tag == 'challenge'
    assert ws.sent == [{'type': 'challenge', 'data': {'challenge': 'abc'}}]
    tag = asyncio.run(_handle_frame(ws, {'type': 'ping', 'data': {'t': 1}},
                                    on_event=lambda d: noop()))
    assert tag == 'pong'
    assert ws.sent[-1] == {'type': 'pong', 'data': {'t': 1}}


def test_handle_frame_event_dispatches():
    from app.integrations.feishu.long_connection import _handle_frame
    ws = _FakeWS()
    got = {}
    async def on_event(data):
        got['data'] = data
    import asyncio
    frame = {'type': 'event', 'data': {'header': {'event_type': 'im.message.receive_v1'},
                                       'event': {'chat_id': 'oc_b'}}}
    tag = asyncio.run(_handle_frame(ws, frame, on_event=on_event))
    assert tag == 'event'
    assert got['data']['event']['chat_id'] == 'oc_b'


def test_runner_skips_when_live_disabled(monkeypatch):
    from app.workers.feishu_long_connection_task import feishu_long_connection
    from app.core.config import settings
    monkeypatch.setattr(settings, 'feishu_live_enabled', False)
    result = feishu_long_connection.apply().get()
    assert result['status'] == 'SKIPPED'
    assert result['reason'] == 'FEISHU_LIVE_DISABLED'


# -- card.action.trigger (v2) / card.action.trigger_v1 (legacy) responses -------


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
