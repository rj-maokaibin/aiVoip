from __future__ import annotations

from app.workers.reproduction_event_tasks import watch_fxs_events


def test_watch_fxs_events_task_is_registered():
    assert watch_fxs_events.name == 'reproduction.watch_fxs_events'


def test_watch_missing_session_returns_not_found():
    # Missing session should return quickly without attempting a device connection.
    result = watch_fxs_events.apply(args=['no-such-session'], throw=False)
    assert result.status == 'SUCCESS'
    assert result.result == {'status': 'SESSION_NOT_FOUND', 'session_id': 'no-such-session',
                             'diagnosis': {'status': 'NO_SESSION', 'session_id': 'no-such-session'}}
