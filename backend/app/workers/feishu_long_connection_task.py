"""Celery resident task: run the Feishu WebSocket long-connection listener.

Runs on its own queue so it does not block other workers. The listener only acts
when FEISHU_LIVE_ENABLED=true and app id/secret are configured; otherwise it
reports SKIPPED and returns. The official lark-oapi SDK keeps the connection
alive with auto-reconnect.
"""
from __future__ import annotations

import time

from celery.utils.log import get_task_logger

from app.core.config import settings
from app.integrations.feishu.long_connection import (
    FeishuLongConnectionError,
    run_long_connection,
)
from app.workers.celery_app import celery_app

log = get_task_logger(__name__)


@celery_app.task(name='feishu.long_connection', bind=True, autoretry_for=(), max_retries=0)
def feishu_long_connection(self, *, run_seconds: float = 0.0):
    """Run the long-connection listener.

    run_seconds <= 0 means run indefinitely (resident worker); a positive value
    bounds the run (used by tests / one-shot diagnostics).
    """
    if not settings.feishu_live_enabled:
        return {'status': 'SKIPPED', 'reason': 'FEISHU_LIVE_DISABLED'}

    try:
        handle = run_long_connection()
    except FeishuLongConnectionError as exc:
        log.warning('feishu long-connection not started: %s', exc)
        return {'status': 'FAILED', 'reason': str(exc)}

    if run_seconds and run_seconds > 0:
        deadline = time.monotonic() + run_seconds
        while time.monotonic() < deadline and handle.is_alive():
            time.sleep(1)
        return {'status': 'STOPPED', 'reason': f'bounded_{run_seconds}s', 'alive': handle.is_alive()}

    while True:
        time.sleep(60)
        if not handle.is_alive():
            log.warning('feishu long-connection thread exited')
            return {'status': 'STOPPED', 'reason': 'thread_exited'}
